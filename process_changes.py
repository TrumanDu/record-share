import re
from typing import List, Optional
import requests
import json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict
import os
import logging
import time
from functools import wraps
from urllib.parse import quote
from urllib.parse import urlparse
from waybackpy import WaybackMachineSaveAPI

# -- configurations begin --
BOOKMARK_COLLECTION_REPO_NAME: str = "record-share"
BOOKMARK_SUMMARY_REPO_NAME: str = "record-share"
MAX_CONTENT_LENGTH: int = 32 * 1024  # 32KB
NO_SUMMARY_TAG: str = "#nosummary"
# -- configurations end --

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(filename)s:%(lineno)d - %(funcName)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def log_execution_time(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        logging.info(f'Entering {func.__name__}')
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        elapsed_time = end_time - start_time
        logging.info(f'Exiting {func.__name__} - Elapsed time: {elapsed_time:.4f} seconds')
        return result
    return wrapper

@dataclass
class SummarizedBookmark:
    month: str  # yyyyMM
    title: str
    url: str
    timestamp: int  # unix timestamp

CURRENT_MONTH: str = datetime.now().strftime('%Y%m')
CURRENT_DATE: str = datetime.now().strftime('%Y-%m-%d')
CURRENT_DATE_AND_TIME: str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

@log_execution_time
def submit_to_wayback_machine(url: str):
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    try:
        save_api = WaybackMachineSaveAPI(url, user_agent)
        wayback_url = save_api.save()
        logging.info(f'Wayback Saved: {wayback_url}')
    except Exception as e:
        # 非关键路径，容忍失败
        logging.warning(f"submit to wayback machine failed, skipping, url={url}")
        logging.exception(e)

@log_execution_time
def get_text_content(url: str) -> str:
        # Check if the URL is a GitHub repository root page
    if url.startswith("https://github.com/"):
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")
        
        # Ensure URL has exactly 2 path components (owner/repo)
        if len(path_parts) == 2:
            owner, repo = path_parts
            # Construct raw README URL
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
            response = requests.get(raw_url)
            
            # Handle valid README response
            if response.status_code == 200:
                content = response.text
                if len(content) > MAX_CONTENT_LENGTH:
                    logging.warning(f"Content length ({len(content)}) exceeds maximum ({MAX_CONTENT_LENGTH}), truncating...")
                    content = content[:MAX_CONTENT_LENGTH]
                return content
            else:
                logging.warning(f"Failed to fetch README.md (HTTP {response.status_code}), falling back to original method")

    jina_url: str = f"https://r.jina.ai/{url}"
    response: requests.Response = requests.get(jina_url)
    content = response.text
    if len(content) > MAX_CONTENT_LENGTH:
        logging.warning(f"Content length ({len(content)}) exceeds maximum ({MAX_CONTENT_LENGTH}), truncating...")
        content = content[:MAX_CONTENT_LENGTH]
    return content

@log_execution_time
def call_openai_api(prompt: str, content: str) -> str:
    model: str = os.environ.get('OPENAI_API_MODEL', 'gpt-4o-mini')
    headers: dict = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "Content-Type": "application/json"
    }
    data: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content}
        ]
    }
    api_endpoint: str = os.environ.get('OPENAI_API_ENDPOINT', 'https://api.openai.com/v1/chat/completions')
    
    # 添加请求相关日志
    logging.info(f"Calling OpenAI API with model: {model}")
    logging.info(f"API endpoint: {api_endpoint}")
    
    response: requests.Response = requests.post(api_endpoint, headers=headers, data=json.dumps(data))
    
    # 添加响应相关日志
    logging.info(f"Response status code: {response.status_code}")
    response_json = response.json()
    logging.debug(f"Response content: {json.dumps(response_json, ensure_ascii=False)}")
    
    # 错误处理
    if response.status_code != 200:
        error_msg = f"OpenAI API request failed with status {response.status_code}"
        logging.error(error_msg)
        logging.error(f"Error response: {response_json}")
        raise Exception(error_msg)
    
    if 'choices' not in response_json:
        error_msg = "Response does not contain 'choices' field"
        logging.error(error_msg)
        logging.error(f"Full response: {response_json}")
        raise Exception(error_msg)
        
    return response_json['choices'][0]['message']['content']

@log_execution_time
def summarize_text(text: str) -> str:
    prompt: str = """
### 角色

假设你是一个IT技术周刊的编辑角色,总结这篇文章。

### 输出要求

1. 要求返回字数在100-200,能够准确描述文章内容。不要输出和给定内容无关的信息。
2. 输出时使用简体中文。
3. 输出时直接给出总结内容，不需要附带“以下是总结”的开始文字或额外的标题。
4. 如果文章内容中有图片，选择一张能表示项目或者产品的截图，一般情况下为文章内容首图，没有图片则不要展示图片。注意分析图片，不要选择项目logo的图片，一般这样的图片命名含有logo字符。
5. 严格按照输出格式。
6. 不要输出例子

### 输出例子

![油桃TV](https://static.trumandu.top/yank-note-picgo-img-20250307214451.png)

油桃 TV 电视浏览器 可看各大卫视 CCTV 直播 无需电视 VIP 适配爱奇艺等主流视频平台

"""
    return call_openai_api(prompt, text)


def slugify(text: str) -> str:
    # replace invalid fs chars with -
    invalid_fs_chars: str = '/\\:*?"<>|'
    return re.sub(r'[' + re.escape(invalid_fs_chars) + r'\s]+', '-', text.lower()).strip('-')

def get_summary_file_path(title: str, timestamp: int, month: Optional[str] = None, in_readme_md: bool = False) -> Path:
    date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
    summary_filename: str = f"{date_str}-{slugify(title)}.md"
    if in_readme_md:
        if month is None:
            raise ValueError("Month must be provided when in_readme_md is True")
        root: Path = Path(".", month)
        summary_filename = f"{date_str}-{quote(slugify(title))}.md"
    else:
        if month is None:
            month = CURRENT_MONTH
        root: Path = Path(BOOKMARK_SUMMARY_REPO_NAME, month)
    summary_path: Path = Path(root, summary_filename)
    return summary_path


def get_monthly_summary_file_path(month: Optional[str] = None) -> Path:
    """获取每月 md 文件路径，如 record-share/202406.md"""
    if month is None:
        month = CURRENT_MONTH
    return Path(BOOKMARK_SUMMARY_REPO_NAME, f"{month}.md")

def build_summary_file(title: str, url: str, summary: str) -> str:
    return f"""### {title}
{url}

{summary}
"""

def build_summary_readme_md(summarized_bookmarks: List[SummarizedBookmark]) -> str:
    initial_prefix: str = """# Bookmark Summary 
读取 bookmark-collection 中的书签，使用 jina reader 获取文本内容，然后使用 LLM 总结文本。详细实现请参见 process_changes.py。需要和 bookmark-collection 中的 Github Action 一起使用。
    
## Summarized Bookmarks
"""
    summary_list: str = ""
    sorted_summarized_bookmarks = sorted(summarized_bookmarks, key=lambda bookmark: bookmark.timestamp, reverse=True)
   
    for bookmark in sorted_summarized_bookmarks:
        summary_file_path = get_summary_file_path(
            title=bookmark.title,
            timestamp=bookmark.timestamp,
            month=bookmark.month,  # 传递书签的月份
            in_readme_md=True
        )
        summary_list += f"- ({datetime.fromtimestamp(bookmark.timestamp).strftime('%Y-%m-%d')}) [{bookmark.title}]({summary_file_path})\n"

    return initial_prefix + summary_list

@log_execution_time
def process_bookmark_file():
    with open(f'{BOOKMARK_COLLECTION_REPO_NAME}/README.md', 'r', encoding='utf-8') as f:
        bookmark_lines: List[str] = f.readlines()

    with open(f'{BOOKMARK_SUMMARY_REPO_NAME}/data.json', 'r', encoding='utf-8') as f:
        summarized_bookmark_dicts = json.load(f)
        summarized_bookmarks = [SummarizedBookmark(**bookmark) for bookmark in summarized_bookmark_dicts]

    summarized_urls = set([bookmark.url for bookmark in summarized_bookmarks])

    # find the first unprocessed && summary-not-present bookmark
    title: Optional[str] = None
    url: Optional[str] = None
    for line in bookmark_lines:
        match: re.Match = re.search(r'- \[(.*?)\]\((.*?)\)', line)
        if match and match.group(2) not in summarized_urls:
            if NO_SUMMARY_TAG in line:
                logging.debug(f"Skipping bookmark with {NO_SUMMARY_TAG} tag: {match.group(1)}")
                continue
            title, url = match.groups()
            break

    if title and url:
        # process the bookmark
        # submit_to_wayback_machine(url)
        text_content: str = get_text_content(url)
        summary: str = summarize_text(text_content)
        summary_file_content: str = build_summary_file(title, url, summary)
        timestamp = int(datetime.now().timestamp())
        

        # 写入到每月 md 文件，若不存在则创建，存在则追加
        monthly_md_path = get_monthly_summary_file_path(CURRENT_MONTH)
        with open(monthly_md_path, 'a', encoding='utf-8') as f:
            f.write(f"\n\n{summary_file_content}\n")
        
        summarized_bookmarks.append(SummarizedBookmark(
            month=CURRENT_MONTH,
            title=title,
            url=url,
            timestamp=timestamp
        ))
        # Update data.json
        with open(f'{BOOKMARK_SUMMARY_REPO_NAME}/data.json', 'w', encoding='utf-8') as f:
            json.dump([asdict(bookmark) for bookmark in summarized_bookmarks], f, indent=2, ensure_ascii=False)

def main():
    process_bookmark_file()

if __name__ == "__main__":
    main()
