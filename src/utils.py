import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
from tqdm import tqdm
import re
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
import pytz
import datetime
from src.gpt_utils import *
from src import config


CURRENT_TIME = datetime.datetime.now(datetime.timezone.utc)
CURRENT_TIMESTAMP = str(CURRENT_TIME.timestamp()).replace(".", "_")
print(f"Current time: {CURRENT_TIMESTAMP}")


def normalize_text(s, sep_token=" \n "):
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r". ,", "", s)
    s = s.replace("..", ".")
    s = s.replace(". .", ".")
    s = s.replace("\n", "")
    s = s.replace("#", "")
    s = s.strip()
    return s


def is_date(string, fuzzy=False):
    """
    Return whether the string can be interpreted as a date.
    :param string: str, string to check for date
    :param fuzzy: bool, ignore unknown tokens in string if True
    """
    try:
        parse(string, fuzzy=fuzzy)
        return True
    except ValueError:
        return False


def preprocess_email(email_body):
    email_body = email_body.split("-------------- next part --------------")[0]
    email_lines = email_body.split('\n')
    temp_ = []
    for line in email_lines:
        if line.startswith("On"):
            line = line.replace("-", " ")
            x = re.sub('\d', ' ', line)
            if is_date(x, fuzzy=True):
                continue
            if line.endswith("> wrote:"):
                continue
        if line.endswith("> wrote:"):
            continue
        if line.startswith("Le "):
            continue
        if line.endswith("?crit :"):
            continue
        if line and not line.startswith('>'):
            if line.startswith('-- ') or line.startswith('[') or line.startswith('_____'):
                continue
            temp_.append(line)
    email_string = "\n".join(temp_)
    normalized_email_string = normalize_text(email_string)
    return normalized_email_string


def scrape_email_data(url_):
    r = requests.get(url_)
    body_soup = BeautifulSoup(r.content, 'html.parser').body
    subject = body_soup.find('h1').text
    author = body_soup.find('b').text
    timestamp = body_soup.find('i').text
    timestamp = parse(str(timestamp), fuzzy=True)
    timestamp = timestamp.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
    email_body = body_soup.find('pre').text
    normalized_email_body = preprocess_email(email_body)
    return author, timestamp, normalize_text(subject), normalized_email_body


def get_past_week_data(dataframe):
    dt_now = CURRENT_TIME
    dt_min = dt_now - datetime.timedelta(days=7)
    dataframe['timestamp'] = pd.to_datetime(dataframe['timestamp'], utc=True)
    sliced_df = dataframe[(dataframe['timestamp'] >= dt_min) & (dataframe['timestamp'] <= dt_now)]
    sliced_df.dropna(inplace=True)
    sliced_df.reset_index(drop=True, inplace=True)
    return sliced_df


def get_datetime_format(dataframe):
    date_list = []
    for i, r in dataframe.iterrows():
        date_string = str(r['date'])
        date_string = date_string.replace("?", " ").strip()
        date_list.append(date_string)
    dataframe['date'] = date_list
    dataframe['date'] = pd.to_datetime(dataframe['date'], utc=True)
    dataframe['date'] = pd.to_datetime(dataframe['date'], format='%Y-%m-%d %H:%M:%S', utc=True)
    dataframe['date'] = dataframe['date'].dt.strftime('%Y-%m-%d %H:%M:%S')
    return dataframe


def collect_email_urls(base_url):
    urls_list = []
    # add current month
    month_route = f"{CURRENT_TIME.strftime('%Y-%B')}"
    email_thread_url = f"{base_url}/{month_route}/"
    urls_list.append(email_thread_url)

    # if current month is not past 7 days, add previous month as well
    if CURRENT_TIME.day < 7:
        prev_month = (CURRENT_TIME - relativedelta(months=1)).strftime('%Y-%B')
        email_thread_url = f"{base_url}/{prev_month}/"
        urls_list.append(email_thread_url)

    all_email_urls = []
    for base_url in urls_list:
        print(f"working on: {base_url}")
        scrape_url = "date.html"
        r = requests.get(base_url + scrape_url)
        soup = BeautifulSoup(r.content, 'html.parser')
        if soup.body:
            ul_soup = soup.body.findAll('ul')[1]
            li_rows = ul_soup.findAll('li')

            # get all emails urls
            email_urls = [base_url + str(i.a['href']).strip() for i in li_rows]
            all_email_urls.extend(email_urls)

    print(f"Fetched Urls: {len(all_email_urls)}")
    return all_email_urls


def scrape_email_urls(email_urls_list):
    df_list = []
    for i in tqdm(email_urls_list):
        auth_, timestamp_, sub_, email_ = scrape_email_data(i)
        df_dict = {
            "timestamp": timestamp_,
            "author": auth_,
            "subject": sub_,
            "email": email_,
            "email_url": i,
        }
        df_list.append(df_dict)
    # data frame of all emails
    emails_df = pd.DataFrame(df_list)

    # filter dataframe to get last week's data only
    df_week = get_past_week_data(emails_df)
    df_week['tokens'] = df_week['email'].apply(lambda x: len(config.TOKENIZER.encode(x)))

    os.makedirs("output", exist_ok=True)
    df_week.to_csv(f"output/df_week_{CURRENT_TIMESTAMP}.csv", index=False)
    return df_week


def get_email_thread_data(sub_df):
    sub_df.sort_values(by='timestamp', ascending=True, inplace=True)
    sub_df.dropna(inplace=True)
    sub_df.reset_index(drop=True, inplace=True)

    first_post_date = ""
    subject = ""
    num_of_replies = sub_df.shape[0]
    author = []
    urls = []
    generated_summary = []
    consolidated_summary = ""
    consolidated_title = ""

    for i, r in tqdm(sub_df.iterrows(), total=sub_df.shape[0]):
        if i == 0:
            first_post_date += r.timestamp.strftime('%Y-%m-%d %H:%M:%S')
            subject += r.subject
        email_text = r.email
        auth = r.author
        url = r.email_url

        if config.CHATGPT:
            summary_ = generate_chatgpt_summary(email_text)
        else:
            summary_ = generate_summary(email_text)

        author.append(auth)
        urls.append(url)
        generated_summary.append(summary_)

    # consolidated summary
    summary_concat = "\n".join(generated_summary)

    if config.CHATGPT:
        consolidated_summary += consolidate_chatgpt_summary(summary_concat)
        consolidated_title += generate_chatgpt_title(summary_concat)
    else:
        consolidated_summary += consolidate_summary(summary_concat)
        consolidated_title += generate_title(summary_concat)

    data_dict = {
        "date": first_post_date,
        "subject": subject,
        "num_replies": num_of_replies,
        "authors": author,
        "urls": urls,
        "generated_summaries": generated_summary,
        "consolidated_title": consolidated_title,
        "consolidated_summary": consolidated_summary
    }
    return data_dict


def generate_newsletter_completion(df):
    grouped_df = df.groupby('subject')
    print(f"Number of threads found: {len(grouped_df)}")
    print("-----")

    data_records = []
    for index, sub_df in grouped_df:
        print(f"working on subject: {index}")
        data_dict = get_email_thread_data(sub_df)
        data_records.append(data_dict)
        print("-----")

    df_week_generated = pd.DataFrame(data_records)
    os.makedirs("output", exist_ok=True)
    df_week_generated.to_csv(f"output/df_week_generated_{CURRENT_TIMESTAMP}.csv", index=False)
    return df_week_generated


def save_html_file(df_week_generated, save_file_name):
    # open html
    file_handle = open(f"output/{save_file_name}", "w")

    html_title = "Sample Newsletter"
    html = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="X-UA-Compatible" content="ie=edge">
    <title>{html_title}</title>
    <link rel="stylesheet" href="style.css">
    </head>
    <body>
    <h1 style="text-align:center; font-family:verdana" >Hello World!</h1>
    <br>
    '''
    file_handle.write(html)

    for idx, row in df_week_generated.iterrows():
        # get data
        subject = row.subject
        date = row.date
        num_replies = row.num_replies
        authors = row.authors
        urls = row.urls
        title = row.consolidated_title
        summary = row.consolidated_summary

        # write subjects and all
        html = f"<hr style='border-top: dotted 2px; '><h2 style='text-align:center; font-family:verdana;'>{subject}</h2><b>Date: </b><i>{date}</i><p>Number of replies: {num_replies}</p>"
        file_handle.write(html)

        # write title and summary
        html = f"<h3 style='text-align:center; font-family:verdana; color:#282828;'>{title}</h3><p>{summary}</p><br><b>References:</b>"
        file_handle.write(html)

        for i in range(len(urls)):
            author = authors[i]
            url = urls[i]
            html = f"<ul><li>{author}: <a href='{url}'>{subject}</a></li></ul>"
            file_handle.write(html)

        html = f"<br>"
        file_handle.write(html)

    html = "</body></html>"
    file_handle.write(html)
    file_handle.close()

    return f"output/{save_file_name}.html"
