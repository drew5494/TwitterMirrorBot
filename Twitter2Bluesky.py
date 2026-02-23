import asyncio
import os
import re
import io
import aiohttp
from lxml import html
from twikit import Client
from atproto_client.utils.text_builder import TextBuilder
from atproto import Client as BlueskyClient
from atproto import models

# --- CONFIGURATION ---
# Define multiple accounts here
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

# Pre-compile regex
SHORT_URL_PATTERN = re.compile(r'https://t.co/[a-zA-Z0-9]+')

# --- METADATA / IMAGE FUNCTIONS ---
async def get_metadata(session, url):
    """Extract OpenGraph metadata using lxml (first 10KB for efficiency)."""
    try:
        async with session.get(url, timeout=10) as response:
            if response.status != 200:
                return None
            html_chunk = await response.content.read(1024 * 10)  # 10 KB only
            tree = html.fromstring(html_chunk)
            title = tree.findtext('.//title') or 'No title'
            description = (tree.xpath('//meta[@property="og:description"]/@content') or
                           tree.xpath('//meta[@name="description"]/@content') or
                           ['No description'])[0]
            thumbnail = (tree.xpath('//meta[@property="og:image"]/@content') or [None])[0]
            del tree
            return {'title': title, 'description': description, 'thumbnail': thumbnail}
    except Exception as e:
        print(f"Metadata extraction warning for {url}: {e}")
        return None

def fix_utm_source(url):
    """If utm_source=twitter, change it to utm_source=bluesky"""
    if not url:
        return url
    # Replace only if utm_source=twitter exists
    return re.sub(r'(utm_source=)twitter', r'\1bluesky', url)

async def fetch_image_to_memory(session, url):
    """Download image to RAM without size limit."""
    try:
        async with session.get(url, timeout=15) as response:
            if response.status == 200:
                data = await response.read()
                return io.BytesIO(data)
    except Exception as e:
        print(f"Image download warning for {url}: {e}")
    return None

# --- MONITOR FUNCTION ---
async def monitor_tweets(session, account):
    twitter_user = account['twitter_user']
    bluesky_user = account['bluesky_user']
    bluesky_pass = account['bluesky_pass']

    # Initialize clients per account
    client = Client('en-US')
    bluesky_client = BlueskyClient()
    previous_tweet_id = None

    # Login
    try:
        if os.path.exists('output_file.json'):
            client.load_cookies('output_file.json')
            print(f"[{twitter_user}] Logged into Twitter (Cookies).")
        bluesky_client.login(bluesky_user, bluesky_pass)
        print(f"[{twitter_user}] Logged into Bluesky.")
    except Exception as e:
        print(f"[{twitter_user}] Login failed: {e}")
        return

    user = await client.get_user_by_screen_name(twitter_user)
    if not user:
        print(f"[{twitter_user}] Twitter user not found.")
        return
    print(f"[{twitter_user}] Monitoring ID: {user.id}")

    # Start monitoring loop
    while True:
        try:
            tweets = await client.get_user_tweets(user.id, 'Tweets', count=1)
            if not tweets:
                await asyncio.sleep(60)
                continue

            latest_tweet = tweets[0]

            if previous_tweet_id == latest_tweet.id:
                await asyncio.sleep(60)
                continue

            print(f"[{twitter_user}] New Tweet: {latest_tweet.text[:50]}...")

            # Extract URL
            full_url = latest_tweet.urls[0].get('expanded_url') if latest_tweet.urls else None
            full_url = fix_utm_source(full_url)
            embed = None

            if full_url:
                metadata = await get_metadata(session, full_url)
                if metadata and metadata['thumbnail']:
                    img_buffer = await fetch_image_to_memory(session, metadata['thumbnail'])
                    if img_buffer:
                        thumb_blob = bluesky_client.upload_blob(img_buffer.getvalue())
                        embed = models.AppBskyEmbedExternal.Main(
                            external=models.AppBskyEmbedExternal.External(
                                title=metadata['title'],
                                description=metadata['description'],
                                uri=full_url,
                                thumb=thumb_blob.blob,
                            )
                        )
                        img_buffer.close()
                        del img_buffer
                        del thumb_blob
                del metadata

            clean_text = SHORT_URL_PATTERN.sub('', latest_tweet.text).strip()
            
            # --- Bluesky character limit check ---
            BLUESKY_LIMIT = 300
            if len(clean_text) > BLUESKY_LIMIT:
                print(f"[{twitter_user}] Tweet too long for Bluesky ({len(clean_text)} chars). Trimming.")
                clean_text = clean_text[:BLUESKY_LIMIT - 3].rstrip() + "..."
            
            tb = TextBuilder()
            tb.text(clean_text)

            try:
                bluesky_client.send_post(tb, embed=embed)
                print(f"[{twitter_user}] Posted to Bluesky successfully.")
                previous_tweet_id = latest_tweet.id
            except Exception as e:
                print(f"[{twitter_user}] Bluesky post error: {e}")

        except Exception as e:
            print(f"[{twitter_user}] Critical Loop Error: {e}")

        await asyncio.sleep(60)

# --- MAIN ---
async def main():
    async with aiohttp.ClientSession() as session:
        tasks = [monitor_tweets(session, account) for account in ACCOUNTS]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping bot...")
