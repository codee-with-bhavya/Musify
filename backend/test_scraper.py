import asyncio
import json
from scrapers.innertube import InnerTube
from scrapers.parsers import parse_search_results, parse_album_page, parse_artist_page

async def main():
    it = InnerTube()

    print("Testing Search...")
    search_res = await it.search("The Weeknd")
    results = parse_search_results(search_res)
    print(f"Found {len(results)} results")
    if results:
        print(f"First result: {results[0]['title']} ({results[0]['type']})")

        # Test Artist Page if found
        artist = next((r for r in results if r['type'] == 'artist'), None)
        if artist:
            print(f"\nTesting Artist Page: {artist['title']}")
            artist_res = await it.browse(artist['browseId'])
            artist_data = parse_artist_page(artist_res)
            print(f"Artist Sections: {[s['title'] for s in artist_data.get('sections', [])]}")

        # Test Album Page if found
        album = next((r for r in results if r['type'] == 'album'), None)
        if album:
            print(f"\nTesting Album Page: {album['title']}")
            album_res = await it.browse(album['browseId'])
            album_data = parse_album_page(album_res)
            print(f"Album Tracks: {len(album_data.get('tracks', []))}")

if __name__ == "__main__":
    asyncio.run(main())
