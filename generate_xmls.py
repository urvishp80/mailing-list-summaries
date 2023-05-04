import os
import re
import pandas as pd
from feedgen.feed import FeedGenerator
from tqdm import tqdm
from elasticsearch import Elasticsearch
import time

from src.gpt_utils import generate_chatgpt_summary
from src.config import TOKENIZER, ES_CLOUD_ID, ES_USERNAME, ES_PASSWORD, ES_INDEX, ES_DATA_FETCH_SIZE
from src.logger import LOGGER


class ElasticSearchClient:
    def __init__(self, es_cloud_id, es_username, es_password, es_data_fetch_size=ES_DATA_FETCH_SIZE) -> None:
        self._es_cloud_id = es_cloud_id
        self._es_username = es_username
        self._es_password = es_password
        self._es_data_fetch_size = es_data_fetch_size
        self._es_client = Elasticsearch(
            cloud_id=self._es_cloud_id,
            http_auth=(self._es_username, self._es_password),
        )

    def extract_data_from_es(self, es_index):
        output_list = []
        start_time = time.time()

        if self._es_client.ping():
            LOGGER.info("connected to the ElasticSearch")
            # Update the query to filter by domain
            query = {
                "query": {
                    "match_phrase": {
                        "domain": "https://lists.linuxfoundation.org/pipermail/bitcoin-dev/"
                    }
                }
            }

            # Initialize the scroll
            scroll_response = self._es_client.search(index=es_index, body=query, size=self._es_data_fetch_size,
                                                     scroll='1m')
            scroll_id = scroll_response['_scroll_id']
            results = scroll_response['hits']['hits']

            # Dump the documents into the json file
            LOGGER.info(f"Starting dumping of {es_index} data in json...")
            # output_data_path = f'{data_path}/{es_index}.json'
            # with open(output_data_path, 'w') as f:
            while len(results) > 0:
                # Save the current batch of results
                for result in results:
                    output_list.append(result)

                # Fetch the next batch of results
                scroll_response = self._es_client.scroll(scroll_id=scroll_id, scroll='1m')
                scroll_id = scroll_response['_scroll_id']
                results = scroll_response['hits']['hits']

            LOGGER.info(
                f"Dumping of {es_index} data in json has completed and has taken {time.time() - start_time:.2f} seconds.")

            return output_list
        else:
            LOGGER.info('Could not connect to Elasticsearch')
            return None


class GenerateXML:
    def __init__(self) -> None:
        self.month_dict = {
            1: "Jan", 2: "Feb", 3: "March", 4: "April", 5: "May", 6: "June",
            7: "July", 8: "Aug", 9: "Sept", 10: "Oct", 11: "Nov", 12: "Dec"
        }

    def check_size_body(self, body):
        tokens = TOKENIZER.encode(body)
        temp = len(tokens) // 3000 + 1 if len(tokens) % 3000 else 0
        bodies = []
        sub_body_size = len(body) // temp
        for i in range(temp):
            s_num = sub_body_size * i
            e_num = (sub_body_size * i) + sub_body_size
            bodies.append(body[s_num:e_num])
        return bodies

    def gpt_api(self, body):
        summ = []
        for b in self.check_size_body(body):
            summ.append(generate_chatgpt_summary(b))
        return "\n".join(summ)

    def create_summary(self, body):
        summ = self.gpt_api(body)
        return summ

    def create_folder(self, month_year):
        os.makedirs(month_year)

    def generate_xml(self, feed_data, xml_file):
        # create feed generator
        fg = FeedGenerator()
        fg.id(feed_data['id'])
        fg.title(feed_data['title'])
        for author in feed_data['authors']:
            fg.author({'name': author})
        fg.link(href=feed_data['base_url'], rel='alternate')
        # add entries to the feed
        fe = fg.add_entry()
        fe.id(feed_data['url'])
        fe.title(feed_data['title'])
        fe.link(href=feed_data['url'], rel='alternate')
        fe.published(feed_data['created_at'])
        fe.summary(feed_data['summary'])

        # generate the feed XML
        feed_xml = fg.atom_str(pretty=True)
        # convert the feed to an XML string
        # write the XML string to a file
        with open(xml_file, 'wb') as f:
            f.write(feed_xml)

    def start(self, dict_data):
        # data = open(json_path, "r")
        # dict_data = []
        # for line in data:
        #     dict_data.append(json.loads(line))

        columns = ['_index', '_id', '_score']
        source_cols = ['body_type', 'created_at', 'id', 'title', 'body', 'type',
                       'url', 'authors']
        df_list = []
        for i in range(len(dict_data)):
            df_dict = {}
            for col in columns:
                df_dict[col] = dict_data[i][col]
            for col in source_cols:
                df_dict[col] = dict_data[i]['_source'][col]
            df_list.append(df_dict)
        emails_df = pd.DataFrame(df_list)

        emails_df['created_at_org'] = emails_df['created_at']
        emails_df['created_at'] = pd.to_datetime(emails_df['created_at'], format="%Y-%m-%dT%H:%M:%S.%fZ")
        result = emails_df.groupby([emails_df['created_at'].dt.month, emails_df['created_at'].dt.year])

        for month_year, email_df in tqdm(result):
            LOGGER.info(f"Working on {month_year}")
            month_name = self.month_dict[int(month_year[0])]
            str_month_year = f"{month_name}_{month_year[1]}"
            if not os.path.exists(f"static/{str_month_year}"):
                self.create_folder(f"static/{str_month_year}")
            count = 0
            for i in email_df.index:
                if count > 5:
                    break
                count += 1
                number = str(email_df.loc[i]['id']).split("-")[-1]
                special_characters = ['/', ':', '@', '#', '$', '*', '&', '<', '>', '\\', '?']
                xml_name = email_df.loc[i]['title']
                xml_name = re.sub(r'[^A-Za-z0-9]+', '-', xml_name)
                for sc in special_characters:
                    xml_name = xml_name.replace(sc, "-")
                LOGGER.info(f"File Name: {xml_name}")
                file_path = f"static/{str_month_year}/{number}_{xml_name}.xml"
                if os.path.exists(file_path):
                    continue
                summary = self.create_summary(email_df.loc[i]['body'])
                feed_data = {
                    'id': email_df.loc[i]['id'],
                    'title': email_df.loc[i]['title'],
                    'base_url': email_df.loc[i]['url'],
                    'authors': email_df.loc[i]['authors'],
                    'url': email_df.loc[i]['url'],
                    'created_at': email_df.loc[i]['created_at_org'],
                    'summary': summary
                }

                self.generate_xml(feed_data, file_path)


if __name__ == "__main__":
    gen = GenerateXML()
    elastic_search = ElasticSearchClient(es_cloud_id=ES_CLOUD_ID, es_username=ES_USERNAME,
                                         es_password=ES_PASSWORD)
    data_list = elastic_search.extract_data_from_es(ES_INDEX)
    gen.start(data_list)
