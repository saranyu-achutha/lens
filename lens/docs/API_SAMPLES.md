# Market Lens: API sample responses

Real responses captured by the application (from `raw_capture` in `data/lens.db`, plus a few live calls on 2026-10-16). Long lists are cut to the first item and long text is truncated; nothing else is changed. API keys and tokens are masked.

| API | Endpoint | Used for |
|---|---|---|
| YouTube Data API v3 | `/channels` | Sources page → “Add channel” (by channel ID or @handle) |
| YouTube Data API v3 | `/playlistItems` | Every YouTube sync (every 6 h) |
| YouTube Data API v3 | `/videos` | Every sync for videos under 7 days old, otherwise once a day, for every video in the window or linked to a song |
| Spotify Web API | `https://accounts.spotify.com/api/token` | Before the first Spotify call of a run, and again when the token expires (1 hour) |
| Spotify Web API | `/search` | Songs step of every pipeline run: one call per YouTube song without a Spotify link |
| Spotify Web API | `/search` | Settings → seed artists: the type-ahead when a user adds a seed artist |
| Spotify Web API | `/search` | Only when the “Spotify discovery” job is started manually (the old label-first approach) |
| Spotify Web API | `/albums/{id}` | Label crawl only (manual) |
| Spotify Web API | `/tracks/{id}` | Label crawl only: the album tracklist has no ISRC, so each new track is looked up once (budget 400 per run) |
| Spotify Web API | `/artists/{id}/albums` | Label crawl only, for each seed artist |
| Deezer API | `https://api.deezer.com/track/isrc:{ISRC}` | Songs step, once a day for every song that has an ISRC |

## YouTube Data API v3

Base URL `https://www.googleapis.com/youtube/v3`. Auth: API key (`key=` parameter; masked as `***` in everything the app stores). Daily quota 10,000 units; the app uses about 30 units per sync.

### channels: channel lookup

- **When it is called:** Sources page → “Add channel” (by channel ID or @handle). Called only when a user adds a channel.
- **Code:** lens/youtube.py `channels()`, lens/studio.py `/api/youtube/channel`
- **Cost:** 1 unit
- **Request:** `GET /channels?part=snippet,statistics,contentDetails&id={channelId}  (or &forHandle=@handle)`
- **Captured URL:** `https://www.googleapis.com/youtube/v3/channels?part=snippet%2Cstatistics%2CcontentDetails&id=UC56gTxNs4f9xZ7Pa2i5xNzg&key=***`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `items[].id` | channel id saved in config.json → youtube.channels |
| `snippet.title` | channel name shown in the Console |
| `statistics.subscriberCount / videoCount` | shown when the channel is added |

**Sample response**

```json
{
  "kind": "youtube#channelListResponse",
  "etag": "UTsZssSO8xe_msm_q-DS7ynvXxU",
  "pageInfo": {
    "totalResults": 1,
    "resultsPerPage": 5
  },
  "items": [
    {
      "kind": "youtube#channel",
      "etag": "5Z6Lad1y1j1JgARmz4MFOnIhvlU",
      "id": "UC56gTxNs4f9xZ7Pa2i5xNzg",
      "snippet": {
        "title": "Sony Music India",
        "description": "Welcome to Sony Music India's official YouTube channel, the ultimate destination for music enthusiasts and fans of the Indian music scene. Immerse yourself in a world of melodies, rhythms, and harmonies as we proudly pre… (truncated)",
        "customUrl": "@sonymusicindia",
        "publishedAt": "2009-09-02T23:16:22Z",
        "thumbnails": {
          "default": {
            "url": "https://yt3.ggpht.com/42o3XIW1j5zmzsFeZs3ND4QwaXXrnpK9vqftcEuEdbgNhPbltXRktcSmyJ12-gSRqKch93Ir=s88-c-k-c0x00ffffff-no-rj",
            "width": 88,
            "height": 88
          },
          "medium": {
            "url": "https://yt3.ggpht.com/42o3XIW1j5zmzsFeZs3ND4QwaXXrnpK9vqftcEuEdbgNhPbltXRktcSmyJ12-gSRqKch93Ir=s240-c-k-c0x00ffffff-no-rj",
            "width": 240,
            "height": 240
          },
          "high": {
            "url": "https://yt3.ggpht.com/42o3XIW1j5zmzsFeZs3ND4QwaXXrnpK9vqftcEuEdbgNhPbltXRktcSmyJ12-gSRqKch93Ir=s800-c-k-c0x00ffffff-no-rj",
            "width": 800,
            "height": 800
          }
        },
        "localized": {
          "title": "Sony Music India",
          "description": "Welcome to Sony Music India's official YouTube channel, the ultimate destination for music enthusiasts and fans of the Indian music scene. Immerse yourself in a world of melodies, rhythms, and harmonies as we proudly pre… (truncated)"
        },
        "country": "IN"
      },
      "contentDetails": {
        "relatedPlaylists": {
          "likes": "",
          "uploads": "UU56gTxNs4f9xZ7Pa2i5xNzg"
        }
      },
      "statistics": {
        "viewCount": "86970097391",
        "subscriberCount": "73300000",
        "hiddenSubscriberCount": false,
        "videoCount": "12656"
      }
    }
  ]
}
```

### playlistItems: list a channel's uploads

- **When it is called:** Every YouTube sync (every 6 h). Reads each channel's “uploads” playlist (UU… = UC… id) newest first, 50 per page, and stops at the first page older than the window; incremental after the first run.
- **Code:** lens/youtube.py `uploads()`, lens/sync_youtube.py `collect_uploads()`
- **Cost:** 1 unit per page of 50
- **Request:** `GET /playlistItems?part=snippet,contentDetails&playlistId=UU{channel}&maxResults=50[&pageToken=…]`
- **Captured URL:** `https://www.googleapis.com/youtube/v3/playlistItems?part=snippet%2CcontentDetails&playlistId=UUT5VwjyDpD5m5qMNagy6Gjw&maxResults=50&pageToken=EAAaHlBUOkNHUWlFRVZGUlROR1JFRTRSa1pGTmpkRU1qRQ&key=***`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `contentDetails.videoId` | platform_item.platform_id (platform = youtube) |
| `contentDetails.videoPublishedAt` | platform_item.published_at (release date of the video) |
| `snippet.title` | platform_item.title; parsed into song / film / artist for grouping and matching |
| `snippet.description` | platform_item.description (credits, used as a matching signal) |
| `(channel name from config)` | platform_item.channel |
| `nextPageToken` | paging |

**Sample response**

```json
{
  "kind": "youtube#playlistItemListResponse",
  "etag": "DQSUSWIc_6hEnb5AjaqfgdLkBEE",
  "nextPageToken": "EAAaH1BUOkNKWUJJaEJET1RGRU1FVkZORU0xTUVOR016RTE",
  "prevPageToken": "EAEaHlBUOkNHUWlFRVZGUlROR1JFRTRSa1pGTmpkRU1qRQ",
  "items": [
    {
      "kind": "youtube#playlistItem",
      "etag": "jn4s0w0q03PNN_ZexMMLnKnWKNc",
      "id": "VVVUNVZ3anlEcEQ1bTVxTU5hZ3k2R2p3LksyLVZUVVdBemdz",
      "snippet": {
        "publishedAt": "2026-07-04T01:30:30Z",
        "channelId": "UCT5VwjyDpD5m5qMNagy6Gjw",
        "title": "Thumri - Kaun Gali Gaye Shyam  | Ras Rang: Evolution of Thumri, Vol. 2  | Longing for Shyam",
        "description": "Immerse yourself in the timeless beauty of \"Kaun Gali Gaye Shyam\", a soulful Thumri from Ras Rang: Evolution of Thumri, Vol. 2. Experience the rich emotions of love and longing through this elegant Hindustani classical m… (truncated)",
        "thumbnails": {
          "default": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/default.jpg",
            "width": 120,
            "height": 90
          },
          "medium": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/mqdefault.jpg",
            "width": 320,
            "height": 180
          },
          "high": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/hqdefault.jpg",
            "width": 480,
            "height": 360
          },
          "standard": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/sddefault.jpg",
            "width": 640,
            "height": 480
          },
          "maxres": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/maxresdefault.jpg",
            "width": 1280,
            "height": 720
          },
          "fhd": {
            "url": "https://i.ytimg.com/vi/K2-VTUWAzgs/fhddefault.jpg",
            "width": 1920,
            "height": 1080
          }
        },
        "channelTitle": "Sony Music India Classical",
        "playlistId": "UUT5VwjyDpD5m5qMNagy6Gjw",
        "position": 100,
        "resourceId": {
          "kind": "youtube#video",
          "videoId": "K2-VTUWAzgs"
        },
        "videoOwnerChannelTitle": "Sony Music India Classical",
        "videoOwnerChannelId": "UCT5VwjyDpD5m5qMNagy6Gjw"
      },
      "contentDetails": {
        "videoId": "K2-VTUWAzgs",
        "videoPublishedAt": "2026-07-04T01:30:30Z"
      }
    },
    "… 49 more item(s) omitted"
  ],
  "pageInfo": {
    "totalResults": 515,
    "resultsPerPage": 50
  }
}
```

### videos: duration and statistics (the trend readings)

- **When it is called:** Every sync for videos under 7 days old, otherwise once a day, for every video in the window or linked to a song. Up to 50 ids per call.
- **Code:** lens/youtube.py `videos()`, lens/sync_youtube.py `take_readings()`
- **Cost:** 1 unit per 50 videos
- **Request:** `GET /videos?part=contentDetails,statistics&id={id1,id2,…up to 50}&maxResults=50`
- **Captured URL:** `https://www.googleapis.com/youtube/v3/videos?part=contentDetails%2Cstatistics&id=zRlPdykmbM8%2CzSlofT7Tqug%2CzT9L3If-ZJA%2CzUGV2NR9aAM%2CzXa5aHOcSLY%2CzXkuYcz0kKQ%2CzYnL5PmYbW0%2CzZWmhBKECnI%2Cz_8EHOYde-E%2Cz_dtSJBRxbI%2CzavM_Lkidvk%2CzeOnDMO9oW8%2CzeSWNskOkmo%2CzeysFMKgnE0%2CzfFUYg83nW0%2CzhJVxi8CyYo%2CzhY_Xmuv-qo%2CzkXXhh3ZFME%2CzlYHlR19Tzw%2Czlb5oKGFQRc%2CzluCin4zJHM%2CzmMNECpZ160%2CznYPZ21ETEA%2CznxCLzh9lAk%2CzoMeu7IiLmM%2CzovzhzRax4Y%2CzpRnpHrZ3qQ%2CzpY5VpuJSdg%2CzpwqFiMUs0s%2Czqjw1_J-4gI%2Czrvik9vwqXI%2CzslQ4zjRqH8%2Czt91Gp5OH1c%2CztNipEXXL_A%2CztpgbjepVLw%2CzwAVsYnPO8Q%2Czw_J1E5AWN8%2Czw_cl4nWbAA%2Czx-JC2Qm3nQ%2CzxIaKGF3iqo%2Czzho2spfBkU%2CzzrTPEtbuCQ&maxResults=50&key=***`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `contentDetails.duration` | platform_item.duration_ms (ISO 8601 → ms); also used for asset type (short / jukebox) |
| `statistics.viewCount` | reading + snapshot, metric = views |
| `statistics.likeCount` | reading + snapshot, metric = likes |
| `statistics.commentCount` | reading + snapshot, metric = comments |

**Sample response**

```json
{
  "kind": "youtube#videoListResponse",
  "etag": "7BTs646E71MFhHQrIXYeDlK3AZs",
  "items": [
    {
      "kind": "youtube#video",
      "etag": "8DjbHv7RFSvJ7QFFAyhvsrUQxi4",
      "id": "zRlPdykmbM8",
      "contentDetails": {
        "duration": "PT23M20S",
        "dimension": "2d",
        "definition": "hd",
        "caption": "false",
        "licensedContent": true,
        "contentRating": {},
        "projection": "rectangular"
      },
      "statistics": {
        "viewCount": "302",
        "likeCount": "4",
        "favoriteCount": "0",
        "commentCount": "0"
      }
    },
    "… 41 more item(s) omitted"
  ],
  "pageInfo": {
    "totalResults": 42,
    "resultsPerPage": 42
  }
}
```

## Spotify Web API

Base URL `https://api.spotify.com/v1`. Auth: client credentials (no user login). The app is in Spotify development mode: search pages hold at most 10 results, batch endpoints return 403, and `label`, `popularity`, `followers` and `genres` are no longer returned.

### token: client-credentials access token

- **When it is called:** Before the first Spotify call of a run, and again when the token expires (1 hour). The token is never stored or shown.
- **Code:** lens/spotify.py `_fetch_token()`
- **Cost:** not counted
- **Request:** `POST https://accounts.spotify.com/api/token  (Basic auth = client id:secret, body grant_type=client_credentials)`
- **Captured URL:** `POST https://accounts.spotify.com/api/token  (grant_type=client_credentials)`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `access_token` | Bearer token for the calls below (kept in memory only) |
| `expires_in` | when to refresh |

**Sample response**

```json
{
  "access_token": "BQD…(redacted)",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### search (type=track): song metadata — the main call

- **When it is called:** Songs step of every pipeline run: one call per YouTube song without a Spotify link. A miss is rechecked after 3 days.
- **Code:** lens/songs.py `spotify_lookup()`
- **Cost:** 1 call per song
- **Request:** `GET /search?q=track:"{song title}"&type=track&market=IN&limit=10&offset=0`
- **Captured URL:** `https://api.spotify.com/v1/search?q=track%3A%22Kaal+Mulaitha+Poovae%22&type=track&market=IN&limit=10&offset=0`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `tracks.items[].id / external_urls.spotify` | platform_item (platform = spotify) and the Spotify link |
| `name` | compared with the YouTube song title (deterministic score) |
| `artists[].name / id` | artist table, and the artist-match signal |
| `duration_ms` | duration-match signal |
| `external_ids.isrc` | track.isrc; also the key for the Deezer lookup |
| `album.name / album_type / release_date / id` | release table; release_date decides “new” vs “catalogue” |
| `explicit` | track.explicit |

**Sample response**

```json
{
  "tracks": {
    "href": "https://api.spotify.com/v1/search?offset=0&limit=10&query=track%3A%22Kaal%20Mulaitha%20Poovae%22&type=track&market=IN",
    "limit": 10,
    "next": null,
    "offset": 0,
    "previous": null,
    "total": 4,
    "items": [
      {
        "album": {
          "album_type": "single",
          "artists": [
            {
              "external_urls": {
                "spotify": "https://open.spotify.com/artist/29aw5YCdIw2FEXYyAJZI8l"
              },
              "href": "https://api.spotify.com/v1/artists/29aw5YCdIw2FEXYyAJZI8l",
              "id": "29aw5YCdIw2FEXYyAJZI8l",
              "name": "Harris Jayaraj",
              "type": "artist",
              "uri": "spotify:artist:29aw5YCdIw2FEXYyAJZI8l"
            }
          ],
          "external_urls": {
            "spotify": "https://open.spotify.com/album/5A2zbvLaMyRtmrdkudyfQl"
          },
          "href": "https://api.spotify.com/v1/albums/5A2zbvLaMyRtmrdkudyfQl",
          "id": "5A2zbvLaMyRtmrdkudyfQl",
          "images": [
            {
              "height": 640,
              "width": 640,
              "url": "https://i.scdn.co/image/ab67616d0000b273360b04bb0a79239fc91f2ffe"
            },
            "… 2 more item(s) omitted"
          ],
          "is_playable": true,
          "name": "Maattrraan (Original Motion Picture Soundtrack)",
          "release_date": "2012-08-10",
          "release_date_precision": "day",
          "total_tracks": 5,
          "type": "album",
          "uri": "spotify:album:5A2zbvLaMyRtmrdkudyfQl"
        },
        "artists": [
          {
            "external_urls": {
              "spotify": "https://open.spotify.com/artist/29aw5YCdIw2FEXYyAJZI8l"
            },
            "href": "https://api.spotify.com/v1/artists/29aw5YCdIw2FEXYyAJZI8l",
            "id": "29aw5YCdIw2FEXYyAJZI8l",
            "name": "Harris Jayaraj",
            "type": "artist",
            "uri": "spotify:artist:29aw5YCdIw2FEXYyAJZI8l"
          },
          "… 4 more item(s) omitted"
        ],
        "disc_number": 1,
        "duration_ms": 330320,
        "explicit": false,
        "external_ids": {
          "isrc": "INS171200564"
        },
        "external_urls": {
          "spotify": "https://open.spotify.com/track/1jR3y5I3SZ7ey8g4OKxodr"
        },
        "href": "https://api.spotify.com/v1/tracks/1jR3y5I3SZ7ey8g4OKxodr",
        "id": "1jR3y5I3SZ7ey8g4OKxodr",
        "is_local": false,
        "is_playable": true,
        "name": "Kaal Mulaitha Poovae",
        "track_number": 5,
        "type": "track",
        "uri": "spotify:track:1jR3y5I3SZ7ey8g4OKxodr"
      },
      "… 3 more item(s) omitted"
    ]
  }
}
```

### search (type=artist): artist picker

- **When it is called:** Settings → seed artists: the type-ahead when a user adds a seed artist.
- **Code:** lens/studio.py `/api/artist_search`
- **Cost:** 1 call per search
- **Request:** `GET /search?q={text}&type=artist&market=IN&limit=…`
- **Captured URL:** `https://api.spotify.com/v1/search` params `{"q": "Arijit Singh", "type": "artist", "market": "IN", "limit": 2}`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `artists.items[].id / name` | saved to seeds/sony_india_artists.txt |
| `images` | thumbnail in the picker |

**Sample response**

```json
{
  "artists": {
    "href": "https://api.spotify.com/v1/search?offset=0&limit=2&query=Arijit%20Singh&type=artist&market=IN",
    "limit": 2,
    "next": "https://api.spotify.com/v1/search?offset=2&limit=2&query=Arijit%20Singh&type=artist&market=IN",
    "offset": 0,
    "previous": null,
    "total": 3,
    "items": [
      {
        "external_urls": {
          "spotify": "https://open.spotify.com/artist/4YRxDV8wJFPHPTeXepOstw"
        },
        "href": "https://api.spotify.com/v1/artists/4YRxDV8wJFPHPTeXepOstw",
        "id": "4YRxDV8wJFPHPTeXepOstw",
        "images": [
          {
            "url": "https://i.scdn.co/image/ab6761610000e5ebadfb0b2df04b77e43b5f7375",
            "height": 640,
            "width": 640
          },
          "… 2 more item(s) omitted"
        ],
        "name": "Arijit Singh",
        "type": "artist",
        "uri": "spotify:artist:4YRxDV8wJFPHPTeXepOstw"
      },
      "… 1 more item(s) omitted"
    ]
  }
}
```

### search (type=album): label crawl (manual, legacy)

- **When it is called:** Only when the “Spotify discovery” job is started manually (the old label-first approach). Also the health probe (limit=1) every 15 minutes while Spotify is failing.
- **Code:** lens/discover_spotify.py, lens/health.py `check_spotify()`
- **Cost:** 1 call per page of 10
- **Request:** `GET /search?q=label:"Sony Music Entertainment India" year:2026&type=album&market=IN&limit=10&offset=…`
- **Captured URL:** `https://api.spotify.com/v1/search?q=label%3A%22Sony+Music+Entertainment+India%22+year%3A2026&type=album&market=IN&limit=10&offset=0`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `albums.items[].id` | album to fetch in full (next section) |
| `name / album_type / release_date / total_tracks` | release table |

**Sample response**

```json
{
  "albums": {
    "href": "https://api.spotify.com/v1/search?offset=0&limit=10&query=label%3A%22Sony%20Music%20Entertainment%20India%22%20year%3A2026&type=album&market=IN",
    "limit": 10,
    "next": null,
    "offset": 0,
    "previous": null,
    "total": 10,
    "items": [
      {
        "album_type": "compilation",
        "total_tracks": 25,
        "is_playable": true,
        "external_urls": {
          "spotify": "https://open.spotify.com/album/3g56VcYTGjfAyGmLt0wiTY"
        },
        "href": "https://api.spotify.com/v1/albums/3g56VcYTGjfAyGmLt0wiTY",
        "id": "3g56VcYTGjfAyGmLt0wiTY",
        "images": [
          {
            "height": 640,
            "url": "https://i.scdn.co/image/ab67616d0000b273b4435ed5bd0ec3cc533359c2",
            "width": 640
          },
          "… 2 more item(s) omitted"
        ],
        "name": "Anirudh Certified Bangers",
        "release_date": "2026-10-15",
        "release_date_precision": "day",
        "type": "album",
        "uri": "spotify:album:3g56VcYTGjfAyGmLt0wiTY",
        "artists": [
          {
            "external_urls": {
              "spotify": "https://open.spotify.com/artist/4zCH9qm4R2DADamUHMCa6O"
            },
            "href": "https://api.spotify.com/v1/artists/4zCH9qm4R2DADamUHMCa6O",
            "id": "4zCH9qm4R2DADamUHMCa6O",
            "name": "Anirudh Ravichander",
            "type": "artist",
            "uri": "spotify:artist:4zCH9qm4R2DADamUHMCa6O"
          }
        ]
      },
      "… 9 more item(s) omitted"
    ]
  }
}
```

### albums/{id}: full album with tracklist

- **When it is called:** Label crawl only (manual). Blocked by Spotify's daily quota at the moment (see errors).
- **Code:** lens/spotify.py `albums()` (one id at a time), lens/discover_spotify.py
- **Cost:** 1 call per album
- **Request:** `GET /albums/{id}?market=IN`
- **Captured URL:** `https://api.spotify.com/v1/albums/0ws4BhBlmjvbpnmseHCEP0?market=IN`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `copyrights[].text (℗ line)` | label check (label field itself is removed in development mode) |
| `external_ids.upc` | release.upc |
| `tracks.items[]` | track table (title, duration, artists, track number); albums with more tracks than are embedded use /albums/{id}/tracks |

**Sample response**

```json
{
  "album_type": "single",
  "total_tracks": 1,
  "is_playable": true,
  "external_urls": {
    "spotify": "https://open.spotify.com/album/0ws4BhBlmjvbpnmseHCEP0"
  },
  "href": "https://api.spotify.com/v1/albums/0ws4BhBlmjvbpnmseHCEP0?market=IN",
  "id": "0ws4BhBlmjvbpnmseHCEP0",
  "images": [
    {
      "url": "https://i.scdn.co/image/ab67616d0000b273c6c88519c1620a2a14c0b680",
      "height": 640,
      "width": 640
    },
    "… 2 more item(s) omitted"
  ],
  "name": "In the Name of Blood (From \"Karathey Babu\")",
  "release_date": "2026-06-20",
  "release_date_precision": "day",
  "type": "album",
  "uri": "spotify:album:0ws4BhBlmjvbpnmseHCEP0",
  "artists": [
    {
      "external_urls": {
        "spotify": "https://open.spotify.com/artist/5VAyiDhBinVfc6RM5RKnLa"
      },
      "href": "https://api.spotify.com/v1/artists/5VAyiDhBinVfc6RM5RKnLa",
      "id": "5VAyiDhBinVfc6RM5RKnLa",
      "name": "Sam C.S.",
      "type": "artist",
      "uri": "spotify:artist:5VAyiDhBinVfc6RM5RKnLa"
    },
    "… 1 more item(s) omitted"
  ],
  "tracks": {
    "href": "https://api.spotify.com/v1/albums/0ws4BhBlmjvbpnmseHCEP0/tracks?offset=0&limit=50&market=IN",
    "limit": 50,
    "next": null,
    "offset": 0,
    "previous": null,
    "total": 1,
    "items": [
      {
        "artists": [
          {
            "external_urls": {
              "spotify": "https://open.spotify.com/artist/5VAyiDhBinVfc6RM5RKnLa"
            },
            "href": "https://api.spotify.com/v1/artists/5VAyiDhBinVfc6RM5RKnLa",
            "id": "5VAyiDhBinVfc6RM5RKnLa",
            "name": "Sam C.S.",
            "type": "artist",
            "uri": "spotify:artist:5VAyiDhBinVfc6RM5RKnLa"
          },
          "… 1 more item(s) omitted"
        ],
        "disc_number": 1,
        "duration_ms": 124704,
        "explicit": false,
        "external_urls": {
          "spotify": "https://open.spotify.com/track/4nRi7EYAtsMLjbOoOI70eB"
        },
        "href": "https://api.spotify.com/v1/tracks/4nRi7EYAtsMLjbOoOI70eB",
        "id": "4nRi7EYAtsMLjbOoOI70eB",
        "is_playable": true,
        "name": "In the Name of Blood (From \"Karathey Babu\")",
        "track_number": 1,
        "type": "track",
        "uri": "spotify:track:4nRi7EYAtsMLjbOoOI70eB",
        "is_local": false
      }
    ]
  },
  "copyrights": [
    {
      "text": "(P) 2026 Sony Music Entertainment India Pvt. Ltd.",
      "type": "P"
    }
  ],
  "external_ids": {
    "upc": "196874538616"
  },
  "genres": []
}
```

### tracks/{id}: single track (ISRC)

- **When it is called:** Label crawl only: the album tracklist has no ISRC, so each new track is looked up once (budget 400 per run).
- **Code:** lens/discover_spotify.py `set_isrc`
- **Cost:** 1 call per track
- **Request:** `GET /tracks/{id}?market=IN`
- **Captured URL:** `https://api.spotify.com/v1/tracks/4qnFfsCaMe2Nsg1VfFPxq9?market=IN`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `external_ids.isrc` | track.isrc (and merging duplicate tracks) |

**Sample response**

```json
{
  "album": {
    "album_type": "single",
    "artists": [
      {
        "external_urls": {
          "spotify": "https://open.spotify.com/artist/1wRPtKGflJrBx9BmLsSwlU"
        },
        "href": "https://api.spotify.com/v1/artists/1wRPtKGflJrBx9BmLsSwlU",
        "id": "1wRPtKGflJrBx9BmLsSwlU",
        "name": "Pritam",
        "type": "artist",
        "uri": "spotify:artist:1wRPtKGflJrBx9BmLsSwlU"
      },
      "… 3 more item(s) omitted"
    ],
    "external_urls": {
      "spotify": "https://open.spotify.com/album/5830mF41OEgnX3twGqEzSL"
    },
    "href": "https://api.spotify.com/v1/albums/5830mF41OEgnX3twGqEzSL",
    "id": "5830mF41OEgnX3twGqEzSL",
    "images": [
      {
        "url": "https://i.scdn.co/image/ab67616d0000b273f636f602313d09930701de59",
        "width": 640,
        "height": 640
      },
      "… 2 more item(s) omitted"
    ],
    "is_playable": true,
    "name": "Bandhu 2.0 (From \"Cocktail 2\")",
    "release_date": "2026-06-17",
    "release_date_precision": "day",
    "total_tracks": 1,
    "type": "album",
    "uri": "spotify:album:5830mF41OEgnX3twGqEzSL"
  },
  "artists": [
    {
      "external_urls": {
        "spotify": "https://open.spotify.com/artist/1wRPtKGflJrBx9BmLsSwlU"
      },
      "href": "https://api.spotify.com/v1/artists/1wRPtKGflJrBx9BmLsSwlU",
      "id": "1wRPtKGflJrBx9BmLsSwlU",
      "name": "Pritam",
      "type": "artist",
      "uri": "spotify:artist:1wRPtKGflJrBx9BmLsSwlU"
    },
    "… 3 more item(s) omitted"
  ],
  "disc_number": 1,
  "duration_ms": 221777,
  "explicit": false,
  "external_ids": {
    "isrc": "INS172605214"
  },
  "external_urls": {
    "spotify": "https://open.spotify.com/track/4qnFfsCaMe2Nsg1VfFPxq9"
  },
  "href": "https://api.spotify.com/v1/tracks/4qnFfsCaMe2Nsg1VfFPxq9",
  "id": "4qnFfsCaMe2Nsg1VfFPxq9",
  "is_local": false,
  "is_playable": true,
  "name": "Bandhu 2.0 (From \"Cocktail 2\")",
  "track_number": 1,
  "type": "track",
  "uri": "spotify:track:4qnFfsCaMe2Nsg1VfFPxq9"
}
```

### artists/{id}/albums: seed artist releases

- **When it is called:** Label crawl only, for each seed artist.
- **Code:** lens/spotify.py `artist_albums()`
- **Cost:** 1 call per page
- **Request:** `GET /artists/{id}/albums?include_groups=album,single,compilation&market=IN&limit=…`
- **Captured URL:** `https://api.spotify.com/v1/artists/4YRxDV8wJFPHPTeXepOstw/albums` params `{"include_groups": "album,single", "market": "IN", "limit": 2}`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `items[].id / release_date` | albums in the date window are fetched in full |

**Sample response**

```json
{
  "href": "https://api.spotify.com/v1/artists/4YRxDV8wJFPHPTeXepOstw/albums?offset=0&limit=2&market=IN&include_groups=album,single",
  "limit": 2,
  "next": "https://api.spotify.com/v1/artists/4YRxDV8wJFPHPTeXepOstw/albums?offset=2&limit=2&market=IN&include_groups=album,single",
  "offset": 0,
  "previous": null,
  "total": 757,
  "items": [
    {
      "album_type": "album",
      "total_tracks": 10,
      "is_playable": true,
      "external_urls": {
        "spotify": "https://open.spotify.com/album/5Er818ARb7me75pPmqlhMK"
      },
      "href": "https://api.spotify.com/v1/albums/5Er818ARb7me75pPmqlhMK",
      "id": "5Er818ARb7me75pPmqlhMK",
      "images": [
        {
          "url": "https://i.scdn.co/image/ab67616d0000b273d48c4f0da042a2b884c3cfa7",
          "height": 640,
          "width": 640
        },
        "… 2 more item(s) omitted"
      ],
      "name": "Happy Vibes",
      "release_date": "2026-07-30",
      "release_date_precision": "day",
      "type": "album",
      "uri": "spotify:album:5Er818ARb7me75pPmqlhMK",
      "artists": [
        {
          "external_urls": {
            "spotify": "https://open.spotify.com/artist/0oOet2f43PA68X5RxKobEy"
          },
          "href": "https://api.spotify.com/v1/artists/0oOet2f43PA68X5RxKobEy",
          "id": "0oOet2f43PA68X5RxKobEy",
          "name": "Shreya Ghoshal",
          "type": "artist",
          "uri": "spotify:artist:0oOet2f43PA68X5RxKobEy"
        },
        "… 3 more item(s) omitted"
      ]
    },
    "… 1 more item(s) omitted"
  ]
}
```

## Deezer API (bonus trend source)

Base URL `https://api.deezer.com`. Free, no key. Rate limit 50 requests per 5 seconds.

### track/isrc:{ISRC}: Deezer rank

- **When it is called:** Songs step, once a day for every song that has an ISRC.
- **Code:** lens/songs.py `deezer_readings()`
- **Cost:** free
- **Request:** `GET https://api.deezer.com/track/isrc:{ISRC}`
- **Captured URL:** `https://api.deezer.com/track/isrc:INS171200515`
- **HTTP status:** 200

**Fields the application uses**

| Field | Stored as / used for |
|---|---|
| `id / link / title / duration / release_date` | platform_item (platform = deezer, matched by ISRC) |
| `rank` | reading + snapshot, metric = rank (Deezer popularity score; higher = more popular; not a stream count) |

**Sample response**

```json
{
  "id": 565581542,
  "readable": false,
  "title": "Onnum Puriyala (From \"Kumki\")",
  "title_short": "Onnum Puriyala (From \"Kumki\")",
  "title_version": "",
  "isrc": "INS171200515",
  "link": "https://www.deezer.com/track/565581542",
  "share": "https://www.deezer.com/track/565581542?utm_source=deezer&utm_content=track-565581542&utm_term=0_1792117640&utm_medium=web",
  "duration": 259,
  "track_position": 8,
  "disk_number": 1,
  "rank": 21370,
  "release_date": "2018-10-12",
  "explicit_lyrics": false,
  "explicit_content_lyrics": 0,
  "explicit_content_cover": 2,
  "preview": "",
  "bpm": 0,
  "gain": -8.6,
  "available_countries": [
    "AE",
    "AF",
    "AG",
    "… 188 more"
  ],
  "contributors": [
    {
      "id": 298792,
      "name": "D. Imman",
      "link": "https://www.deezer.com/artist/298792",
      "share": "https://www.deezer.com/artist/298792?utm_source=deezer&utm_content=artist-298792&utm_term=0_1792117640&utm_medium=web",
      "picture": "https://api.deezer.com/artist/298792/image",
      "picture_small": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/56x56-000000-80-0-0.jpg",
      "picture_medium": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/250x250-000000-80-0-0.jpg",
      "picture_big": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/500x500-000000-80-0-0.jpg",
      "picture_xl": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/1000x1000-000000-80-0-0.jpg",
      "radio": true,
      "tracklist": "https://api.deezer.com/artist/298792/top?limit=50",
      "type": "artist",
      "role": "Main"
    },
    "… 1 more item(s) omitted"
  ],
  "md5_image": "9d0a4444751a49e9b4b4cb5a848e9765",
  "track_token": "AAAAAWrRi4hq0qTIvaEd3oYJAYv53H_EM_Us1tBYoQjYebwNGqmdTOaV8KGP9bWflGsHtpQocG45cq2ocEs_VvLGgDG_KbZwrvvuHBJaPM2WOmOfnl3Xd2NhSn-xQmxL-MtrDxTFABSCorNerDq3PdBX0A4bSwR7K4FYKRJxJFPJNa2X4g1aPGF73tcy3_cptzawcEMpq0UObiTAZxQisl51dH9i… (truncated)",
  "artist": {
    "id": 298792,
    "name": "D. Imman",
    "link": "https://www.deezer.com/artist/298792",
    "share": "https://www.deezer.com/artist/298792?utm_source=deezer&utm_content=artist-298792&utm_term=0_1792117640&utm_medium=web",
    "picture": "https://api.deezer.com/artist/298792/image",
    "picture_small": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/56x56-000000-80-0-0.jpg",
    "picture_medium": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/250x250-000000-80-0-0.jpg",
    "picture_big": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/500x500-000000-80-0-0.jpg",
    "picture_xl": "https://cdn-images.dzcdn.net/images/artist/b7f064e7e78b9bf3eb1ac62839c731cd/1000x1000-000000-80-0-0.jpg",
    "radio": true,
    "tracklist": "https://api.deezer.com/artist/298792/top?limit=50",
    "type": "artist"
  },
  "album": {
    "id": 75163312,
    "title": "Mazhaiyum Naanum: Kondattam",
    "link": "https://www.deezer.com/album/75163312",
    "cover": "https://api.deezer.com/album/75163312/image",
    "cover_small": "https://cdn-images.dzcdn.net/images/cover/9d0a4444751a49e9b4b4cb5a848e9765/56x56-000000-80-0-0.jpg",
    "cover_medium": "https://cdn-images.dzcdn.net/images/cover/9d0a4444751a49e9b4b4cb5a848e9765/250x250-000000-80-0-0.jpg",
    "cover_big": "https://cdn-images.dzcdn.net/images/cover/9d0a4444751a49e9b4b4cb5a848e9765/500x500-000000-80-0-0.jpg",
    "cover_xl": "https://cdn-images.dzcdn.net/images/cover/9d0a4444751a49e9b4b4cb5a848e9765/1000x1000-000000-80-0-0.jpg",
    "md5_image": "9d0a4444751a49e9b4b4cb5a848e9765",
    "release_date": "2018-10-12",
    "tracklist": "https://api.deezer.com/album/75163312/tracks",
    "type": "album"
  },
  "type": "track"
}
```

## Error responses the application handles

### 429 QUOTA_EXCEEDED: daily quota for an endpoint group

Album and track lookups are currently blocked (Retry-After ≈ 23.6 h). Search keeps working. The app records the retry time on the Sources page and does not retry until then.

Captured URL: `https://api.spotify.com/v1/albums/0ws4BhBlmjvbpnmseHCEP0/tracks`

```json
{
  "error": {
    "status": 429,
    "message": "Too many requests",
    "reason": "QUOTA_EXCEEDED"
  }
}
```

### 403 Forbidden: batch endpoints in development mode

`/albums?ids=…` and `/tracks?ids=…` are refused for development-mode apps, so the app fetches one item at a time.

Captured URL: `https://api.spotify.com/v1/albums?ids=45Q1OXnKdOS2B7TkinNudO%2C5830mF41OEgnX3twGqEzSL%2C5xahy2Tzi8r1KbjVeOM0fd%2C0GN8c8LKElLxzjr5gOams3%2C7xkNinsFBJgny5p4XZZfBS%2C6aGQLlNbYzOwTfjWDpqg58%2C1PBCVDJauHi37GO4NkboT2%2C05M6vAqjgC8YC7RV29AKqE%2C4o1kT6L2yEINJ1OR2o4phN%2C2WqjkiwpP8Ks3qMt41epWo%2C05jrzEkewvqM1WAWD07bKI%2C713AvxcNnVOUQnp4LRcc5G%2C1ptkIUrvlqwD2ToCjP3LoR%2C5TaLWTSvmek5AHglJXuecr%2C1ttzMgG7D1bPl2adfo1lbD%2C1amMxIOiRmuujw24rXrCq0%2C01UrMORJHZ5AUvPSdwNXhM%2C6jAXkfJWR7IJlWdjJ5V3lR%2C2V4mtnrcdJ2AnaxueHFlAb%2C6TTQVaiOWeUZIdnF1voJRJ&market=IN`

```json
{
  "error": {
    "status": 403,
    "message": "Forbidden"
  }
}
```

### 400 Invalid limit: search page size

Development mode allows at most 10 results per search page. The app drops the page size from 50 to 10 automatically.

Captured URL: `https://api.spotify.com/v1/search?q=label%3A%22Sony+Music+India%22+year%3A2026&type=album&market=IN&limit=50&offset=0`

```json
{
  "error": {
    "status": 400,
    "message": "Invalid limit"
  }
}
```

---
© 2026 Saranyu Technologies. All rights reserved.
