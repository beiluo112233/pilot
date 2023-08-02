# user_stories.py
from termcolor import colored
from const.function_calls import ARCHITECTURE

from utils.utils import execute_step, find_role_from_step, generate_app_data, step_already_finished
from database.database import save_progress, get_progress_steps
from logger.logger import logger
from prompts.prompts import get_additional_info_from_user
from helpers.AgentConvo import AgentConvo


def get_architecture(high_level_summary, user_stories, user_tasks, args):
    current_step = 'architecture'
    convo_architecture = AgentConvo(current_step)

    # If this app_id already did this step, just get all data from DB and don't ask user again
    step = get_progress_steps(args['app_id'], current_step)
    if step and not execute_step(args['step'], current_step):
        step_already_finished(args, step)
        return step['architecture']

    # ARCHITECTURE
    print(colored(f"Planning project architecture...\n", "green"))
    logger.info(f"Planning project architecture...")

    architecture = convo_architecture.send_message('architecture/technologies.prompt',
        {'prompt': high_level_summary,
        'user_stories': user_stories,
        'user_tasks': user_tasks,
        'app_type': args['app_type']}, ARCHITECTURE)

    architecture = get_additional_info_from_user(architecture, 'architect')

    logger.info(f"Final architecture: {architecture}")

    save_progress(args['app_id'], current_step, {
        "messages": convo_architecture.get_messages(),
        "architecture": architecture,
        "app_data": generate_app_data(args)
    })

    return architecture
    # ARCHITECTURE END
