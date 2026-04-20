import asyncio
import os
import re
import random
import aiohttp
from lxml import html
from twikit import Client
from atproto_client.utils.text_builder import TextBuilder
from atproto import Client as BlueskyClient
from atproto import models

# --- CONFIGURATION ---
ACCOUNTS = [
    {
        'twitter_user': os.getenv('TWITTER_USER1', ''),
        'bluesky_user': os.getenv('BLUESKY_USER1', ''),
        'bluesky_pass': os.getenv('BLUESKY_PASS1', '')
    },
    {
        'twitter_user': os.getenv('TWITTER_USER2', ''),
        'bluesky_user': os.getenv('BLUESKY_USER2', ''),
        'bluesky_pass': os.getenv('BLUESKY_PASS2', '')
    }
]

MAX_CHARS = 300
ELLIPSIS = "…"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

SHORT_URL_PATTERN = re.compile(r'https://t.co/[a-zA-Z0-9]+')

# --- HELPERS ---

async def get_metadata(session, url):
    """Extract OpenGraph metadata using limited HTML read."""
    try:
        async with session.get(url, timeout=10, headers=HEADERS) as response:
            if response.status != 200:
                return None

            html_chunk = await response.content.read(32 * 1024)
            tree = html.fromstring(html_chunk)

            title = (tree.xpath('//meta[@property="og:title"]/@content') or
                     [tree.findtext('.//title') or 'No title'])[0]

            description = (tree.xpath('//meta[@property="og:description"]/@content') or
                           tree.xpath('//meta[@name="description"]/@content') or
                           ['No description'])[0]

            thumbnail = (tree.xpath('//meta[@property="og:image"]/@content') or [None])[0]

            return {
                'title': title.strip(),
                'description': description.strip(),
                'thumbnail': thumbnail
            }

    except Exception as e:
        print(f"Metadata warning for {url}: {e}")
        return None


def truncate_at_word_boundary(text, max_chars=300):
    if len(text) <= max_chars:
        return text

    cutoff = max_chars - len(ELLIPSIS)
    truncated = text[:cutoff]
    last_space = truncated.rfind(" ")

    if last_space == -1:
        return truncated + ELLIPSIS

    return truncated[:last_space].rstrip() + ELLIPSIS


def fix_utm_source(url):
    if not url:
        return url
    return re.sub(r'(utm_source=)twitter', r'\1bluesky', url)


async def fetch_image(session, url):
    """Download image as raw bytes (no BytesIO)."""
    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        print(f"Image download warning: {e}")
    return None


# --- MONITOR ---

async def monitor_tweets(session, account):
    t_user = account['twitter_user']
    b_user = account['bluesky_user']
    b_pass = account['bluesky_pass']

    if not b_user or not b_pass:
        print(f"[{t_user}] Missing Bluesky credentials. Skipping.")
        return

    twitter_client = Client('en-US')
    bsky_client = BlueskyClient()

    previous_tweet_id = None

    try:
        if os.path.exists('output_file.json'):
            twitter_client.load_cookies('output_file.json')

        bsky_client.login(b_user, b_pass)
        print(f"[{t_user}] Logged into both platforms.")

        user_info = await twitter_client.get_user_by_screen_name(t_user)
        if not user_info:
            print(f"[{t_user}] User not found.")
            return

    except Exception as e:
        print(f"[{t_user}] Auth Error: {e}")
        return

    while True:
        try:
            tweets = await twitter_client.get_user_tweets(
                user_info.id, 'Tweets', count=1
            )

            if not tweets:
                await asyncio.sleep(60)
                continue

            latest_tweet = tweets[0]

            # Initial run
            if previous_tweet_id is None:
                previous_tweet_id = latest_tweet.id
                print(f"[{t_user}] Monitoring started.")
                await asyncio.sleep(60)
                continue

            # Skip duplicates
            if previous_tweet_id == latest_tweet.id:
                await asyncio.sleep(random.uniform(50, 70))
                continue

            print(f"[{t_user}] New Tweet: {latest_tweet.id}")

            full_url = (
                latest_tweet.urls[0].get('expanded_url')
                if latest_tweet.urls else None
            )
            full_url = fix_utm_source(full_url)

            embed = None

            if full_url:
                metadata = await get_metadata(session, full_url)

                if metadata and metadata['thumbnail']:
                    img_bytes = await fetch_image(session, metadata['thumbnail'])

                    if img_bytes:
                        thumb_blob = bsky_client.upload_blob(img_bytes)

                        embed = models.AppBskyEmbedExternal.Main(
                            external=models.AppBskyEmbedExternal.External(
                                title=metadata['title'],
                                description=metadata['description'],
                                uri=full_url,
                                thumb=thumb_blob.blob,
                            )
                        )

            clean_text = SHORT_URL_PATTERN.sub('', latest_tweet.text).strip()
            clean_text = truncate_at_word_boundary(clean_text, MAX_CHARS)

            tb = TextBuilder()
            tb.text(clean_text)

            try:
                bsky_client.send_post(tb, embed=embed)
                print(f"[{t_user}] Posted to Bluesky.")
                previous_tweet_id = latest_tweet.id

            except Exception as e:
                print(f"[{t_user}] Post error: {e}")

        except Exception as e:
            print(f"[{t_user}] Loop Error: {e}")
            await asyncio.sleep(120)

        await asyncio.sleep(random.uniform(55, 65))


# --- MAIN ---

async def main():
    connector = aiohttp.TCPConnector(limit=20)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [monitor_tweets(session, acc) for acc in ACCOUNTS]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping bot safely...")