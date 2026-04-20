import asyncio
import os
import re
import io
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
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

# Pre-compile regex
SHORT_URL_PATTERN = re.compile(r'https://t.co/[a-zA-Z0-9]+')

# --- HELPER FUNCTIONS ---

async def get_metadata(session, url):
    """Extract OpenGraph metadata. Increased chunk size to 64KB for news sites."""
    try:
        async with session.get(url, timeout=10, headers=HEADERS) as response:
            if response.status != 200:
                return None
            
            # News sites often have large <head> sections; 64KB is safer than 10KB
            html_chunk = await response.content.read(64 * 1024)
            tree = html.fromstring(html_chunk)
            
            title = (tree.xpath('//meta[@property="og:title"]/@content') or 
                     [tree.findtext('.//title') or 'No title'])[0]
            
            description = (tree.xpath('//meta[@property="og:description"]/@content') or
                           tree.xpath('//meta[@name="description"]/@content') or
                           ['No description'])[0]
            
            thumbnail = (tree.xpath('//meta[@property="og:image"]/@content') or [None])[0]
            
            return {'title': title.strip(), 'description': description.strip(), 'thumbnail': thumbnail}
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
    if not url: return url
    return re.sub(r'(utm_source=)twitter', r'\1bluesky', url)

async def fetch_image_to_memory(session, url):
    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                return io.BytesIO(await response.read())
    except Exception as e:
        print(f"Image download warning: {e}")
    return None

# --- MONITOR FUNCTION ---

async def monitor_tweets(session, account):
    t_user = account['twitter_user']
    b_user = account['bluesky_user']
    b_pass = account['bluesky_pass']

    twitter_client = Client('en-US')
    bsky_client = BlueskyClient()
    previous_tweet_id = None

    try:
        if os.path.exists('output_file.json'):
            twitter_client.load_cookies('output_file.json')
        
        bsky_client.login(b_user, b_pass)
        print(f"[{t_user}] Logged into both platforms.")
        
        # Get Twitter User ID
        user_info = await twitter_client.get_user_by_screen_name(t_user)
        if not user_info:
            print(f"[{t_user}] Error: User not found.")
            return
    except Exception as e:
        print(f"[{t_user}] Auth Error: {e}")
        return

    while True:
        try:
            tweets = await twitter_client.get_user_tweets(user_info.id, 'Tweets', count=1)
            if not tweets:
                await asyncio.sleep(60)
                continue

            latest_tweet = tweets[0]

            # 1. Initial run: Store ID but don't post to avoid "historical flood"
            if previous_tweet_id is None:
                previous_tweet_id = latest_tweet.id
                print(f"[{t_user}] Initialization successful. Monitoring for new posts...")
                await asyncio.sleep(60)
                continue

            # 2. Skip if it's the same tweet
            if previous_tweet_id == latest_tweet.id:
                # Jitter: sleep between 50-70 seconds so requests aren't perfectly rhythmic
                await asyncio.sleep(random.uniform(50, 70))
                continue

            print(f"[{t_user}] New Tweet detected: {latest_tweet.id}")

            # Extract and Clean Metadata
            full_url = latest_tweet.urls[0].get('expanded_url') if latest_tweet.urls else None
            full_url = fix_utm_source(full_url)
            embed = None

            if full_url:
                metadata = await get_metadata(session, full_url)
                if metadata and metadata['thumbnail']:
                    img_buffer = await fetch_image_to_memory(session, metadata['thumbnail'])
                    if img_buffer:
                        thumb_blob = bsky_client.upload_blob(img_buffer.getvalue())
                        embed = models.AppBskyEmbedExternal.Main(
                            external=models.AppBskyEmbedExternal.External(
                                title=metadata['title'],
                                description=metadata['description'],
                                uri=full_url,
                                thumb=thumb_blob.blob,
                            )
                        )
            
            # Post Content
            clean_text = SHORT_URL_PATTERN.sub('', latest_tweet.text).strip()
            clean_text = truncate_at_word_boundary(clean_text, MAX_CHARS)
            
            tb = TextBuilder()
            tb.text(clean_text)

            try:
                bsky_client.send_post(tb, embed=embed)
                print(f"[{t_user}] Mirrored to Bluesky.")
                previous_tweet_id = latest_tweet.id
            except Exception as e:
                print(f"[{t_user}] Bluesky post error: {e}")

        except Exception as e:
            print(f"[{t_user}] Loop Error: {e}")
            await asyncio.sleep(120) # Back off on error

        await asyncio.sleep(random.uniform(55, 65))

# --- MAIN ---

async def main():
    async with aiohttp.ClientSession() as session:
        tasks = [monitor_tweets(session, acc) for acc in ACCOUNTS]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping bot safely...")