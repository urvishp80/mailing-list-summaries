import re
import pandas as pd
from elasticsearch import Elasticsearch
import time
import traceback
import openai
from datetime import datetime, timedelta
from loguru import logger
import xml.etree.ElementTree as ET
import os
from dotenv import load_dotenv
import sys
import warnings
import pytz
import json

import nltk

# nltk.download('punkt')
# from nltk.tokenize import sent_tokenize

from src.utils import preprocess_email
from src.gpt_utils import generate_chatgpt_summary, consolidate_chatgpt_summary
from src.config import TOKENIZER, ES_CLOUD_ID, ES_USERNAME, ES_PASSWORD, ES_INDEX, ES_DATA_FETCH_SIZE
from generate_homepage_xml import ElasticSearchClient, GenerateJSON

import numpy as np

warnings.filterwarnings("ignore")
load_dotenv()

# if set to True, it will use chatgpt model ("gpt-3.5-turbo") for all the completions
CHATGPT = True

# COMPLETION_MODEL - only applicable if CHATGPT is set to False
OPENAI_ORG_KEY = os.getenv("OPENAI_ORG_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.organization = OPENAI_ORG_KEY
openai.api_key = OPENAI_API_KEY

ACTIVE_POSTS = True
RECENT_POSTS = False


if __name__ == "__main__":

    gen = GenerateJSON()
    elastic_search = ElasticSearchClient(es_cloud_id=ES_CLOUD_ID, es_username=ES_USERNAME,
                                         es_password=ES_PASSWORD)
    dev_urls = [
        "https://lists.linuxfoundation.org/pipermail/bitcoin-dev/",
        "https://lists.linuxfoundation.org/pipermail/lightning-dev/"
    ]

    current_date_str = None
    if not current_date_str:
        current_date_str = datetime.now().strftime("%Y-%m-%d")

    start_date = datetime.now() - timedelta(days=7)
    start_date_str = start_date.strftime("%Y-%m-%d")

    end_date = datetime.now() - timedelta(days=1)
    end_date_str = end_date.strftime("%Y-%m-%d")

    logger.info(f"current_date_str: {current_date_str}")
    logger.info(f"Gathering data for newsletter from {start_date_str} to {end_date_str}")

    active_data_list = []
    new_threads_list = []

    for dev_url in dev_urls:
        all_data_df, all_data_list = elastic_search.fetch_all_data_for_url(ES_INDEX, dev_url, start_date_str, end_date_str)
        data_list = elastic_search.extract_data_from_es(ES_INDEX, dev_url, start_date_str, end_date_str)
        dev_name = dev_url.split("/")[-2]
        logger.success(f"Total threads received for {dev_name}: {len(data_list)}")

        # TOP ACTIVE POSTS
        seen_titles = set()
        active_posts_data = elastic_search.filter_top_active_posts(es_results=data_list, top_n=10,
                                                                   all_data_df=all_data_df)
        active_posts_data_counter = 0
        for data in active_posts_data:
            if active_posts_data_counter >= 3:
                break

            title = data['_source']['title']
            if title in seen_titles:
                continue
            seen_titles.add(title)
            counts, contributors = elastic_search.fetch_contributors_and_threads(title=title, domain=dev_url,
                                                                                 df=all_data_df)
            # get the first post's info of this title
            df_title = all_data_df.loc[(all_data_df['title'] == title) & (all_data_df['domain'] == dev_url)]
            df_title.sort_values(by='created_at', inplace=True)
            original_post = df_title.iloc[0].to_dict()

            for i in all_data_list:
                if i['_source']['title'] == original_post['title'] and i['_source']['domain'] == original_post[
                    'domain'] and i['_source']['authors'] == original_post['authors'] and i['_source']['created_at'] == \
                        original_post['created_at'] and i['_source']['url'] == original_post['url']:
                    for author in i['_source']['authors']:
                        contributors.remove(author)
                    i['_source']['n_threads'] = counts
                    i['_source']['contributors'] = contributors
                    i['_source']['dev_name'] = dev_name
                    active_data_list.append(i)
                    active_posts_data_counter += 1
                    break
        logger.info(f"number of active posts collected: {len(active_data_list)}")

        # NEW THREADS POSTS
        for i in data_list:
            if i['_source']['type'] == 'original_post':
                title = i['_source']['title']
                counts, contributors = elastic_search.fetch_contributors_and_threads(title=title, domain=dev_url,
                                                                                     df=all_data_df)
                authors = i['_source']['authors']
                for author in authors:
                    contributors.remove(author)
                i['_source']['n_threads'] = counts
                i['_source']['contributors'] = contributors
                i['_source']['dev_name'] = dev_name
                new_threads_list.append(i)
        logger.info(f"number of new threads started: {len(new_threads_list)}")

    # gather ids of docs from json file
    json_file_path = r"static/newsletter.json"
    current_directory = os.getcwd()
    json_full_path = os.path.join(current_directory, json_file_path)
    json_xml_ids = set()
    if os.path.exists(json_full_path):
        with open(json_full_path, 'r') as j:
            json_data = json.load(j)
        json_xml_ids = set(
            [item['title'] for item in json_data['new_threads_this_week']] +
            [item['title'] for item in json_data['active_posts_this_week']]
        )
    else:
        logger.warning(f"No existing homepage.json file found: {json_full_path}")

    # gather ids of docs from active posts and new thread posts
    filtered_docs_ids = set(
        [gen.get_id(data['_source']['title']) for data in active_data_list] +
        [gen.get_id(data['_source']['title']) for data in new_threads_list]
    )

    # check if there are any updates in xml file
    if filtered_docs_ids != json_xml_ids:
        logger.info("changes found in recent posts ... ")

        delay = 5
        count = 0

        while True:
            try:
                logger.success(f"Total no. of active posts collected: {len(active_data_list)}")
                logger.success(f"Total no. of new threads started this week: {len(new_threads_list)}")

                logger.info("creating newsletter.json file ... ")
                if len(active_data_list) > 0 or len(new_threads_list) > 0:
                    json_file_name = "newsletter.json"
                    new_threads_summary = gen.generate_recent_posts_summary(new_threads_list)

                    new_threads_page_data = []
                    for data in new_threads_list:
                        entry_data = gen.create_single_entry(data, look_for_combined_summary=True)
                        new_threads_page_data.append(entry_data)

                    active_page_data = []
                    for data in active_data_list:
                        entry_data = gen.create_single_entry(data, look_for_combined_summary=True)
                        active_page_data.append(entry_data)

                    json_string = {
                         "summary_of_threads_started_this_week": new_threads_summary,
                         "new_threads_this_week": new_threads_page_data,
                         "active_posts_this_week": active_page_data
                    }

                    f_name = f"static/{json_file_name}"
                    with open(f_name, 'w') as f:
                        f.write(json.dumps(json_string, indent=4))
                        logger.success(f"json saved file: {f_name}")

                else:
                    logger.error(f"Data list empty! Please check the data again.")

                break
            except Exception as ex:
                logger.error(f"Error occurred: {ex} \n{traceback.format_exc()}")
                time.sleep(delay)
                count += 1
                if count > 5:
                    sys.exit(ex)
    else:
        logger.success("No change in the posts, no need to update newsletter.json file")
