# Support for Instagram Highlight links `/s/...`

> Archived design. Gokapi, referenced in the original delivery flow below, has since been removed. Current delivery uses the local Telegram Bot API.

## Goal

Handle published Instagram links of the form `https://www.instagram.com/s/<token>?story_media_id=<id>`. Nuvio should convert the link to a canonical Highlight URL, find the specified item, and download only that item. If it cannot retrieve the media, the user should receive a short, useful message without details about cookies or the bot account.

## Verified yt-dlp behavior

The following was verified with yt-dlp 2026.07.04:

- The Instagram extractor does not recognize the original `/s/...` path.
- The Base64URL token in the tested link decodes to the `highlight:<numeric id>` form.
- `InstagramStory` recognizes the corresponding canonical Highlight URL. The numeric ID is withheld because it identifies an unrelated creator's media.
- With cookie authentication, the extractor returned 19 items.
- The item matching the requested `story_media_id` was found and downloaded separately.
- The tested file was MP4, H.264 + AAC, 720×1280, and 14.9 seconds long.

## Scope

Support only published `/s/...` links whose token decodes to `highlight:<numeric id>` and whose `story_media_id` parameter contains a numeric media ID.

Ordinary `/stories/<user>/<id>` links and links to an entire Highlight without `story_media_id` are outside this scope. Existing behavior for posts, Reels, photo posts, and Instagram Audio remains the same.

## Design

### Parse the link

Add a private `/s/...` link parser in `utils/tiktok_instagram_utils.py`. It should:

1. Accept only the exact host `instagram.com` or `www.instagram.com`.
2. Extract exactly one path segment after `/s/`.
3. Decode the Base64URL token safely, restoring padding when needed.
4. Accept only a result of the form `highlight:<numeric id>`.
5. Extract a numeric `story_media_id`.
6. Return the canonical Highlight URL, the original `story_media_id`, and the item's shortcode computed with yt-dlp's `_pk_to_id`.

Do not pass damaged or incomplete `/s/...` links to yt-dlp's Generic extractor.

### Retrieve metadata

`get_instagram_info()` should recognize `/s/...` before the normal Instagram branch:

1. Convert the link to a canonical Highlight URL.
2. Request the Highlight through yt-dlp with the project's Instagram cookie file.
3. Find the entry whose `id` matches the shortcode derived from `story_media_id`.
4. Return that entry as a single media item.
5. Add the canonical URL, entry position, original numeric ID, and Highlight-share marker to internal result fields.

Store those fields with `video_info` in the existing callback session. No separate database or migration is needed.

### Download

When `cached_info` has the Highlight-share marker, `download_instagram_video()` should:

1. Replace the original `/s/...` URL with the saved canonical URL.
2. Pass the saved `playlist_items` value to yt-dlp so it downloads only the selected position.
3. Confirm that yt-dlp returned the expected shortcode.
4. Continue the existing file processing flow, including codec checks and the Gokapi delivery step described at the time of this design.

`download_instagram_audio()` already calls `download_instagram_video()` with the same `cached_info`, so audio does not need a separate Highlight-item selection path.

## Errors and user messages

The approved user message was:

> Could not retrieve this item from Instagram. The link may require authorization, the item may have been deleted, or the link may have expired. Try another link to the item.

Keep the message in `messages.py`. Do not expose cookie state, a need to update cookies, bot-account subscriptions, or internal API/extractor responses to users.

Use this message when the `/s/...` token or `story_media_id` is invalid, the Highlight is unavailable with current authorization, the selected entry has disappeared, the link or material is no longer available, Instagram changes its response, or yt-dlp cannot retrieve or download the selected item.

Keep the exact underlying error, cookie state, and traceback in logs and the administrator crash report. Expected unavailability should have its own error category instead of `IG-UNKNOWN`.

The `igsh` parameter is unnecessary and should be omitted from the normalized URL. The callback session uses the canonical URL without `igsh`.

## Compatibility

- Public posts, Reels, carousels, and photo posts keep their existing paths.
- `/stories/...` still receives the existing unsupported-Stories response.
- Without the project's Instagram cookie file, `/s/...` ends with the approved user message.
- Browser cookies were used only for diagnosis and must not become a production dependency.

## Tests

Regression tests should cover:

1. Valid `/s/...` decoding and canonical URL construction.
2. Tokens without Base64 padding.
3. Damaged tokens, an incorrect prefix, and missing `story_media_id`.
4. Selection of an entry that is not first in the playlist.
5. A missing requested entry.
6. Cookie and `playlist_items` use during download.
7. Verification of the downloaded item's shortcode.
8. The approved user message without cookie or subscription details.
9. An expected-unavailability category instead of `IG-UNKNOWN`.
10. Unchanged behavior for ordinary Instagram posts, Reels, and `/stories/...`.

Run targeted tests, `ruff check .`, and the full `pytest` suite.
