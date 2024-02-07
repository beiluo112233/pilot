import os.path
import re
from typing import Optional
from traceback import format_exc
from difflib import unified_diff

from helpers.AgentConvo import AgentConvo
from helpers.Agent import Agent
from helpers.files import get_file_contents
from const.function_calls import GET_FILE_TO_MODIFY, REVIEW_CHANGES

from utils.exit import trace_code_event
from utils.telemetry import telemetry

# Constant for indicating missing new line at the end of a file in a unified diff
NO_EOL = "\ No newline at end of file"

# Regular expression pattern for matching hunk headers
PATCH_HEADER_PATTERN = re.compile(r"^@@ -(\d+),?(\d+)? \+(\d+),?(\d+)? @@")

MAX_REVIEW_RETRIES = 3

class CodeMonkey(Agent):
    save_dev_steps = True

    def __init__(self, project):
        super().__init__('code_monkey', project)

    def get_original_file(
            self,
            code_changes_description: str,
            step: dict[str, str],
            files: list[dict],
        ) -> tuple[str, str]:
        """
        Get the original file content and name.

        :param code_changes_description: description of the code changes
        :param step: information about the step being implemented
        :param files: list of files to send to the LLM
        :return: tuple of (file_name, file_content)
        """
        # If we're called as a result of debugging, we don't have the name/path of the file
        # to modify so we need to figure that out first.
        if 'path' not in step or 'name' not in step:
            file_to_change = self.identify_file_to_change(code_changes_description, files)
            step['path'] = os.path.dirname(file_to_change)
            step['name'] = os.path.basename(file_to_change)

        rel_path, abs_path = self.project.get_full_file_path(step['path'], step['name'])

        for f in files:
            # Take into account that step path might start with "/"
            if (f['path'] == step['path'] or (os.path.sep + f['path'] == step['path'])) and f['name'] == step['name'] and f['content']:
                file_content = f['content']
                break
        else:
            # If we didn't have the match (because of incorrect or double use of path separators or similar), fallback to directly loading the file
            try:
                file_content = get_file_contents(abs_path, self.project.root_path)['content']
                if isinstance(file_content, bytes):
                    # We should never want to change a binary file, but if we do end up here, let's not crash
                    file_content = "... <binary file, content omitted> ..."
            except ValueError:
                # File doesn't exist, we probably need to create a new one
                file_content = ""

        file_name = os.path.join(rel_path, step['name'])
        return file_name, file_content

    def implement_code_changes(
        self,
        convo: Optional[AgentConvo],
        step: dict[str, str],
    ) -> AgentConvo:
        """
        Implement code changes described in `code_changes_description`.

        :param convo: conversation to continue)
        :param step: information about the step being implemented
        """
        code_change_description = step['code_change_description']

        standalone = False
        if not convo:
            standalone = True
            convo = AgentConvo(self)

        files = self.project.get_all_coded_files()
        file_name, file_content = self.get_original_file(code_change_description, step, files)

        # Get the new version of the file
        content = self.replace_complete_file(
            convo,
            standalone,
            code_change_description,
            file_content,
            file_name,
            files,
        )

        if content and content != file_content:
            content = self.format_file(file_name, content)

        # Review the changes and only apply changes that are useful/approved
        if content and content != file_content:
            content = self.review_change(convo, code_change_description, file_name, file_content, content)

        # If we have changes, update the file
        # TODO: if we *don't* have changes, we might want to retry the whole process (eg. have the reviewer
        # explicitly reject the whole PR and use that as feedback in implement_changes.prompt)
        if content and content != file_content:
            if not self.project.skip_steps:
                delta_lines = len(content.splitlines()) - len(file_content.splitlines())
                telemetry.inc("created_lines", delta_lines)
            self.project.save_file({
                'path': step['path'],
                'name': step['name'],
                'content': content,
            })

        return convo

    def replace_complete_file(
        self,
        convo: AgentConvo,
        standalone: bool,
        code_changes_description: str,
        file_content: str,
        file_name: str,
        files: list[dict]
    ) -> str:
        """
        As a fallback, replace the complete file content.

        This should only be used if we've failed to replace individual code blocks.

        :param convo: AgentConvo instance
        :param standalone: True if this is a standalone conversation
        :param code_changes_description: description of the code changes
        :param file_content: content of the file being updated
        :param file_name: name of the file being updated
        :param files: list of files to send to the LLM
        :return: updated file content

        Note: if even this fails for any reason, the original content is returned instead.
        """
        llm_response = convo.send_message('development/implement_changes.prompt', {
            "full_output": True,
            "standalone": standalone,
            "code_changes_description": code_changes_description,
            "file_content": file_content,
            "file_name": file_name,
            "files": files,
        })
        start_pattern = re.compile(r"^\s*```([a-z0-9]+)?\n")
        end_pattern = re.compile(r"\n```\s*$")
        llm_response = start_pattern.sub("", llm_response)
        llm_response = end_pattern.sub("", llm_response)
        convo.remove_last_x_messages(2)
        return llm_response

    def identify_file_to_change(self, code_changes_description: str, files: list[dict]) -> str:
        """
        Identify file to change based on the code changes description

        :param code_changes_description: description of the code changes
        :param files: list of files to send to the LLM
        :return: file to change
        """
        convo = AgentConvo(self)
        llm_response = convo.send_message('development/identify_files_to_change.prompt', {
            "code_changes_description": code_changes_description,
            "files": files,
        }, GET_FILE_TO_MODIFY)
        return llm_response["file"]

    def format_file(
        self,
        file_name: str,
        content: str,
    ) -> str:
        """
        Format the file using an available formatter, if any.
        """
        def prettier(name: str) -> list[str]:
            return ["npx", "prettier", "--stdin-filepath", name]

        FORMATTERS = {
            ".js": prettier,
            ".json": prettier,
            ".ts": prettier,
            ".jsx": prettier,
            ".vue": prettier,
            ".tsx": prettier,
            ".css": prettier,
            ".scss": prettier,
            ".py": lambda name: ["ruff", "format", "--stdin-filename", name],
            ".go": lambda _: ["gofmt"],
        }

        _, file_ext = os.path.splitext(file_name)
        formatter = FORMATTERS.get(file_ext, None)
        if not formatter:
            return

        from subprocess import check_output, CalledProcessError
        format_cmd = formatter(file_name)
        try:
            return check_output(format_cmd, input=content, text=True)
        except FileNotFoundError:
            # formatter not installer, silently ignore and do nothing
            return content
        except CalledProcessError as e:
            raise ValueError(f"Syntax check failed with:\n{e.stderr}")


    def review_change(
        self,
        convo: AgentConvo,
        instructions: str,
        file_name: str,
        old_content: str,
        new_content: str
    ) -> str:
        """
        Review changes that were applied to the file.

        This asks the LLM to act as a PR reviewer and for each part (hunk) of the
        diff, decide if it should be applied (kept) or ignored (removed from the PR).

        :param convo: AgentConvo instance
        :param instructions: instructions for the reviewer
        :param file_name: name of the file being modified
        :param old_content: old file content
        :param new_content: new file content (with proposed changes)
        :return: file content update with approved changes

        Diff hunk explanation: https://www.gnu.org/software/diffutils/manual/html_node/Hunks.html
        """

        hunks = self.get_diff_hunks(file_name, old_content, new_content)

        llm_response = convo.send_message('development/review_changes.prompt', {
            "instructions": instructions,
            "file_name": file_name,
            "old_content": old_content,
            "hunks": hunks,
        }, REVIEW_CHANGES)
        messages_to_remove = 2

        for i in range(MAX_REVIEW_RETRIES):
            ids_to_apply = set()
            ids_to_ignore = set()
            for hunk in llm_response.get("hunks", []):
                if hunk.get("decision", "").lower() == "apply":
                    ids_to_apply.add(hunk["number"] - 1)
                elif hunk.get("decision", "").lower() == "ignore":
                    ids_to_ignore.add(hunk["number"] - 1)

            n_hunks = len(hunks)
            n_review_hunks = len(ids_to_apply | ids_to_ignore)
            if n_review_hunks == n_hunks:
                break
            elif n_review_hunks < n_hunks:
                error = "Not all hunks have been reviewed. Please review all hunks and add 'apply' or 'ignore' decision for each."
            elif n_review_hunks > n_hunks:
                error = f"Your review contains more hunks ({n_review_hunks}) than in the original diff ({n_hunks}). Note that one hunk may have multiple changed lines."

            # Max two retries; if the reviewer still hasn't reviewed all hunks, we'll just use the entire new content
            llm_response = convo.send_message(
                'utils/llm_response_error.prompt', {
                    "error": error
                },
                REVIEW_CHANGES,
            )
            messages_to_remove += 2
        else:
            # The reviewer failed to review all the hunks in 3 attempts, let's just use all the new content
            convo.remove_last_x_messages(messages_to_remove)
            return new_content

        convo.remove_last_x_messages(messages_to_remove)

        hunks_to_apply = [ h for i, h in enumerate(hunks) if i in ids_to_apply ]
        diff_log = f"--- {file_name}\n+++ {file_name}\n" + "\n".join(hunks_to_apply)

        if len(hunks_to_apply) == len(hunks):
            print("Applying entire change")
            return new_content
        elif len(hunks_to_apply) == 0:
            print("Reviewer has doubts, but applying all proposed changes anyway")
            trace_code_event(
                "modify-file-review-reject-all",
                {
                    "file": file_name,
                    "original": old_content,
                    "diff": f"--- {file_name}\n+++ {file_name}\n" + "\n".join(hunks),
                }
            )
            return new_content
        else:
            print("Applying code change:\n" + diff_log)
            return self.apply_diff(file_name, old_content, hunks_to_apply, new_content)

    @staticmethod
    def get_diff_hunks(file_name: str, old_content: str, new_content: str) -> list[str]:
        """
        Get the diff between two files.

        This uses Python difflib to produce an unified diff, then splits
        it into hunks that will be separately reviewed by the reviewer.

        :param file_name: name of the file being modified
        :param old_content: old file content
        :param new_content: new file content
        :return: change hunks from the unified diff
        """
        from_name = "old_" + file_name
        to_name = "to_" + file_name
        from_lines = old_content.splitlines(keepends=True)
        to_lines = new_content.splitlines(keepends=True)
        diff_gen = unified_diff(from_lines, to_lines, fromfile=from_name, tofile=to_name)
        diff_txt = "".join(diff_gen)

        hunks = re.split(r'\n@@', diff_txt, re.MULTILINE)
        result = []
        for i, h in enumerate(hunks):
            # Skip the prologue (file names)
            if i == 0:
                continue
            txt = h.splitlines()
            txt[0] = "@@" + txt[0]
            result.append("\n".join(txt))
        return result

    def apply_diff(
        self,
        file_name: str,
        old_content: str,
        hunks: list[str],
        fallback: str
    ):
        """
        Apply the diff to the original file content.

        This uses the internal `_apply_patch` method to apply the
        approved diff hunks to the original file content.

        If patch apply fails, the fallback is the full new file content
        with all the changes applied (as if the reviewer approved everythng).

        :param file_name: name of the file being modified
        :param old_content: old file content
        :param hunks: change hunks from the unified diff
        :param fallback: proposed new file content (with all the changes applied)
        """
        diff = "\n".join(
            [
                "--- " + file_name,
                "+++ " + file_name,
            ] + hunks
        ) + "\n"
        try:
            fixed_content = self._apply_patch(old_content, diff)
        except Exception as e:
            # This should never happen but if it does, just use the new version from
            # the LLM and hope for the best
            print(f"Error applying diff: {e}; hoping all changes are valid")
            trace_code_event(
                "patch-apply-error",
                {
                    "file": file_name,
                    "error": str(e),
                    "traceback": format_exc(),
                    "original": old_content,
                    "diff": diff
                }
            )
            return fallback

        return fixed_content

    # Adapted from https://gist.github.com/noporpoise/16e731849eb1231e86d78f9dfeca3abc (Public Domain)
    @staticmethod
    def _apply_patch(original: str, patch: str, revert: bool = False):
        """
        Apply a patch to a string to recover a newer version of the string.

        :param original: The original string.
        :param patch: The patch to apply.
        :param revert: If True, treat the original string as the newer version and recover the older string.
        :return: The updated string after applying the patch.
        """
        original_lines = original.splitlines(True)
        patch_lines = patch.splitlines(True)

        updated_text = ''
        index_original = start_line = 0

        # Choose which group of the regex to use based on the revert flag
        match_index, line_sign = (1, '+') if not revert else (3, '-')

        # Skip header lines of the patch
        while index_original < len(patch_lines) and patch_lines[index_original].startswith(("---", "+++")):
            index_original += 1

        while index_original < len(patch_lines):
            match = PATCH_HEADER_PATTERN.match(patch_lines[index_original])
            if not match:
                raise Exception("Bad patch -- regex mismatch [line " + str(index_original) + "]")

            line_number = int(match.group(match_index)) - 1 + (match.group(match_index + 1) == '0')

            if start_line > line_number or line_number > len(original_lines):
                raise Exception("Bad patch -- bad line number [line " + str(index_original) + "]")

            updated_text += ''.join(original_lines[start_line:line_number])
            start_line = line_number
            index_original += 1

            while index_original < len(patch_lines) and patch_lines[index_original][0] != '@':
                if index_original + 1 < len(patch_lines) and patch_lines[index_original + 1][0] == '\\':
                    line_content = patch_lines[index_original][:-1]
                    index_original += 2
                else:
                    line_content = patch_lines[index_original]
                    index_original += 1

                if line_content:
                    if line_content[0] == line_sign or line_content[0] == ' ':
                        updated_text += line_content[1:]
                    start_line += (line_content[0] != line_sign)

        updated_text += ''.join(original_lines[start_line:])
        return updated_text
