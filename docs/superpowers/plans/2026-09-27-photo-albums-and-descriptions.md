# Albums and Descriptions of Instagram and TikTok Posts

Date: 2026-09-27. Status: Local implementation completed and verified. Acceptance through this Bot API and Telegram clients remains a separate step.

## 1. The Result for the User

Photos from a single post are grouped into albums while preserving the original order. The user selects whether to attach a description to a video or photo post. A long recipe is preserved in full. Re-clicking or delivery errors should not automatically generate duplicate copies of already sent content.

The main content includes single videos, single photos, and Instagram and TikTok photo posts. Instagram mixed carousels (containing both photos and videos) are included in a separate step: they cannot be considered fully supported until the composition and order are verified.

Audio-only mode retains its original purpose: the description selection is valid only for the current request, does not become a permanent user setting, and does not affect other links or platforms.

## 2. Grounds and Limits of the Examination

### Limitations of Telegram

- `sendMediaGroup` accepts 2–10 elements. Photos and videos can be combined; documents are grouped only with documents, and audio files only with audio files. A successful response includes a message for each element, so the album appears as a visual group rather than a single `Message` object. [Bot API: sendMediaGroup](https://core.telegram.org/bots/api#sendmediagroup)
- The caption for a photo or video is limited to 1024 characters after formatting is applied. In PTB 22.8, the overall album description is placed on the first element. [PTB: InputMediaPhoto](https://docs.python-telegram-bot.org/en/v22.8/telegram.inputmediaphoto.html), [PTB: send_media_group](https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html#telegram.Bot.send_media_group)
- Individual text messages are limited to 4096 characters after formatting. A long description will require additional messages. [Bot API: sendMessage](https://core.telegram.org/bots/api#sendmessage)
- The PTB accepts either a URL or a `file_id`. The suitability of a URL for Telegram must be checked independently of whether the URL is accessible to the Nuvio server. [PTB: InputMediaPhoto](https://docs.python-telegram-bot.org/en/v22.8/telegram.inputmediaphoto.html)

### Verified on 2026-09-27

Metadata checks were performed using Nuvio's current features on public links, without cookies and without downloading media files. The source URLs are withheld because they identify unrelated creators; these measurements are historical and require fresh public examples for reproduction.

| Example | Result |
|--------|--------|
| TikTok photo post (URL withheld) | 12 images; `itemStruct.desc` is recognized as a 65-character description |
| TikTok video (URL withheld) | Contains "description", 64 characters |
| Instagram Reel (URL withheld) | Contains "description", 22 characters |
| Instagram photo post (URL withheld) | Backup processor received 3 images and "description", 165 characters |

Additionally:
- Python 3.14.4 and PTB 22.8 are installed in the local environment.
- Verified the call of `Bot.send_media_group` using a substituted HTTP transport: two photos are serialized in a single request, the common caption is applied only to the first element, and the result is split into two messages.
- After completing the classifier, 863 tests passed. `ruff check .` passed, and measured coverage was 72%, above the 70% threshold.
- Added local tests for text splitting, buttons, albums, partial failures, cancellation, double clicking, cache handling, mixed carousels, and error classification.
- The actual sending of a new album, long description, and reception in Telegram clients has not yet been performed.
The four examples confirm the availability of the text, but do not guarantee its availability or completeness for any publication.

### Points of Change Affected by the Implementation

| File | Implemented Behavior |
|------|------------------------|
| [social_delivery.py](../../../utils/social_delivery.py) | General rules for description status, UTF-16 limits, media order preservation, album grouping, and delivery result |
| [telegram_utils.py](../../../utils/telegram_utils.py), `_build_main_menu` | Added choices to include or omit the description, description status, mixed post label, and title length limit |
| `_deliver_photo_post_by_url`, `_send_photo_post_assets` | URLs and files are sent in sequential groups; messages, confirmed items, audio, and partial failures are recorded |
| `_handle_main_callback`, `_deliver_by_url`, `send_single_file` | One description choice is used when sending from cache, per URL, and per file; an unknown origin does not trigger re-sending |
| [tiktok_instagram_utils.py](../../../utils/tiktok_instagram_utils.py), `_build_tiktok_photo_info` | `itemStruct.desc` is highlighted as post text; unchecked `tikwm_title` is displayed as inaccessible description |
| `_extract_instagram_description`, `_build_instagram_photo_info` | GraphQL caption extraction is retained; HTML metadata cannot enable the description button when the text is incomplete |
| `_instagram_carousel_items`, `download_instagram_photo_post_assets` | The mixed carousel preserves the photo/video order, validates the full composition, and uses existing video preparation |
| [platform_actions.py](../../../utils/platform_actions.py) | Old actions are preserved; actions with descriptions clearly use the `direct_video` key |
| [callback_fsm.py](../../../utils/callback_fsm.py) and `telegram_utils.py` | The callback format is saved; busy sessions are considered during cleanup and to prevent session crowding |
| [messages.py](../../../messages.py) | Added button captions, error statuses, cancellations, uploaded photos, and descriptions |

## 3. User Logic

### The Menu

If a valid, non-empty description has already been obtained, display two buttons, each on a separate line:
1. "Download without description"
2. "Download with description"

The content type is shown on the card: video, photo post, or mixed post. The "Only Audio" button remains available under current conditions, followed by "Cancel". For photo posts without sound, the audio button does not appear.
If the source clearly returns an empty description, display the standard "Download video" or "Download post" button along with the message "The post has no description". If the description cannot be retrieved, use the message "Description not available". Do not combine the blank text case with extraction errors - treat them as separate status conditions.

Retrieving already available metadata occurs before the menu is displayed. Do not perform additional network requests solely to fetch a description for each post that lacks one. Reuse responses from prior metadata retrieval and existing fallback paths.

This is especially important for TikTok, where long text may be present in the `title` field. The menu message should not be overloaded with excessive text even before the user selects a download option.

### Split photos into albums

The order of elements matches the publication order. There is no parallel sending between albums in the same operation. Posts from different links are never merged.

| Number of photos | Sending method |
|-----------------|----------------|
| 0 | Report retrieval failure; do not send an empty request to Telegram |
| 1 | One photo sent via `sendPhoto` |
| 2–10 | Sent as a single album |
| 11 | Sent as 9 + 2 albums |
| 12 | Sent as 10 + 2 albums |
| 20 | Sent as 10 + 10 albums |
| 21 | Sent as 10 + 9 + 2 albums |

**Rule**: Group elements in sets of up to 10. If the remainder after grouping is 1, move the last element from the previous group to the final group.

Audio photo posts are sent as a separate message after the visual media and description. The number of albums does not equal the number of successful download events - each user request is counted only once.

### Placing the description

| Description length and presence | Behavior |
|-------------------------------|----------|
| No description | Media is sent without a caption; no separate message containing the post text |
| Description ≤ 1024 characters | Description is attached to the video, single photo, or the first element of the first album |
| Description > 1024 characters | Full text is sent in individual messages, each up to 4096 characters, in response to the first media sent |

The description appears only once for the entire post. Do not split it across different photo captions or truncate it. For long media descriptions, do not repeat text fragments - instead, send the full text, which is easy to copy.

Preserve paragraphs, lists, links, hashtags, and emojis. Send the text with explicit `parse_mode=None` so that original characters are not converted into Telegram markup. Normalize line endings, remove unacceptable control characters, do not rewrite the content, and do not automatically add the author or link.

Split long text first by paragraphs, then by words. Divide a long continuous line at a secure Unicode boundary without losing characters. Length calculations must account for emoji; a conservative boundary in UTF-16 units is acceptable if it does not alter the text. The exact boundary is further verified against the real API.

If the first media is deleted before the text is sent, and the rejection is confirmed due to missing message, resend only the text without referencing the deleted media. Save the original chat or topic parameters in advance to ensure the reply does not end up in a different chat or topic.

## 4. Extracting the description and post composition

Normalize the post analysis into an ordered list of media items, the description text, its availability and source, optional separate audio, and whether the full post composition was verified.

Description rules:

1. For an ordinary video, use a non-empty `description` from its metadata.
2. For a TikTok photo post, carry the original `itemStruct.desc` into the description. A synthetic title such as "TikTok photo post ..." is not a description.
3. Treat TikWM `title` as a candidate for the post text, but check whether it is complete when a longer description is available. A non-empty title alone does not prove completeness.
4. For Instagram, use `caption.text` or `edge_media_to_caption`. For a playlist, prefer the post description; do not concatenate captions from different entries or use arbitrary text from one child item.
5. HTML `og:description` may contain likes, an author name, and a truncated excerpt. Do not present it as a complete recipe. Mark the description unavailable when the full text cannot be reliably extracted.
6. Do not replace a missing description with an ordinary video title.

Freeze the text and media composition for the selected operation. A repeated network lookup must not silently change the recipe or the order after sending has begun.

For mixed Instagram posts, keep photos and videos in one ordered sequence. Each video must pass the existing size, compatibility, and geometry checks. Put `InputMediaPhoto` and `InputMediaVideo` in the album in the original order. A video preview is not a post photo.

Compare the number and sequence of item types from the sidecar and the yt-dlp playlist. The Instagram extractor builds `entries` in `carousel_media` order and fills video formats from `video_versions`. If counts or item types differ, mark the composition incomplete and stop before the first send. [Instagram extractor in yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/instagram.py).

An HTML fallback that returns only a cover or part of a carousel does not prove that the post is complete. Stop before sending when incompleteness is known. If completeness cannot be checked, record that limitation explicitly instead of treating a non-empty list as proof.

## 5. Delivery, cache, and state

### General operation path

1. Validate session, action, and platform.
2. Reserve the session before the first hold to prevent parallel delivery from rapid clicks.
3. Plan elements, groups, and text before the first send.
4. For photos: preserve existing size limits, allowlist, and CDN failover memory.
5. Send visual blocks sequentially, saving the confirmed `message_id` and `media_group_id` after each Telegram reply.
6. Send a separate description if needed, followed by audio.
7. Display the total delivered parts, record the result, and release temporary files when no longer in use.

### Local albums

- Use supported PTB paths with the local Bot API or file objects via the regular API.
- Avoid loading the entire post into memory if not required.
- Files must remain accessible until the end of the request and be closed at output.

If the photo does not meet Telegram's requirements, preserve the option to send a document. The document cannot be mixed with the photo in the same album: assign it to a separate section, maintain the original order, and group adjacent compatible elements. Before sending, verify known format and size restrictions. The `BadRequest` error alone does not indicate a problem with the photo format.

### Cache and Callback

- Both description choices use the existing `direct_video` key. The description belongs to the new message, not the saved file. No additional video copy or SQLite migration is needed.
- The description must be taken from session metadata and included when sending a cached `file_id`, when sending a URL, or when uploading the file. Transitioning from URL to file should not alter the selected text.
- Photo albums are not yet integrated into the single video/audio cache. Storing the `file_id` will require a separate contract and is outside the scope of this task.
- Retain the old `tiktok_download` and `instagram_download` actions as options without a description. Add explicit `tiktok_download_desc` and `instagram_download_desc` actions used by the common delivery handler.
- Store the format as `s|token|main|action`, validate the callback limit of 64 bytes, and ensure the action matches the session platform.
- The selection is stored within a specific operation. Do not use a shared switch in `context.user_data` that could affect a neighboring request.

### Errors and Delivery Confirmation

For the operation, track progress on visual blocks, description messages, and audio. Result states: complete delivery, partial delivery, cancellation, and unknown outcome of the last request. Successfully confirmed parts are not resent.

| Situation | Decision |
|---------|----------|
| Telegram refused to accept the URL until the first successful group | Proceed to files for unsent content |
| The URL is rejected in a subsequent group | Save previously confirmed groups; skip to files for residual content only |
| The limit of requests with `retry_after` has been exceeded | Account for the specified pause within the limited retry budget; preserve the ability to cancel |
| Error in format, caption, or chat access | Address the specific cause; do not initiate downloading if there is any error in Telegram |
| Timeout or disconnect after sending begins | Result is unknown; stop sending automatically and do not blindly retry the group |
| Photos or videos are sent, but text is not delivered | Continue independent audio submission; indicate that the description was not sent |
| Album and description are sent, but audio is not delivered | Show partial result; do not resend the album or description |
| Unable to update the status message or write analytics | Do not consider an already confirmed delivery as unsuccessful |

Allow the repeat button only for portions with a confirmed rejection. For unknown outcomes, explain that the message may have arrived; do not allow the normal download button to imperceptibly repeat the entire post. Preserve the status of already-sent portions for the duration of the session, while clearing unnecessary temporary files. If new CDN links are needed for a repeat, verify the IDs and order of the elements against the saved plan. Do not automatically continue with a modified post composition on top of previously sent elements.
In the existing `delivery` analytics, record one success after confirming the full requested media, including any separate audio if it was part of the post.
The log should include the platform, session, block number, type of delivery, and output - not the full recipe, cookies, or signed URLs with secret parameters.

### Cancellations and Parallel Requests

Check for cancellation before starting each download, each album, each text segment, and each audio segment. A Telegram request that has already begun sending may end with transmission - cancellation stops subsequent actions but does not guarantee deletion of content already received by the user. After partial delivery, the cancellation message should reflect the content that was already sent; stating "nothing downloaded" in this case is incorrect.

Session occupancy must account for content sent via URL and `file_id` when the temporary directory is empty. An active session cannot be terminated simply by the absence of files. A new lock is valid for one session and does not prevent the processing of another link. Multiple queries from different users can alternate in the chat, but their media and descriptions must remain tied to the original publication.

After restarting the bot, progress stored in memory is lost. This plan does not include automatic resumption of delivery. It is not possible to guarantee the absence of duplicates in the case of a new manual request or in the case of an unknown Telegram response without an additional verification mechanism. Within the scope of this task, automatic retry of such requests is excluded.

## 6. Implementation Plan

### Stage 1: Metadata and Clean Rules

- [x] Record the overall analysis of the post description: text, source, availability.
- [x] Normalize TikTok photo posts and Instagram content, excluding synthetic headlines and HTML snippets.
- [x] Identify pure functions for breaking media and preparing caption/text components.
- [x] Verify text integrity and element order in edge cases.

Readiness: The rules operate independently of Telegram and do not require changes to the database.

### Stage 2: Photo Albums and Reliable Delivery

- [x] Combine URL-based and disk-based sending through a shared group plan.
- [x] Support sending a single photo and groups of 2–10 photos, preserving the original order.
- [x] Save separate audio files and send incompatible photo documents.
- [x] Return from low-level sending confirmed messages and outputs: delivered, rejected, or output unknown.
- [x] Implement progress tracking by parts, distinguish between failure and unknown outcomes, and safely repeat remaining parts.
- [x] Add protection against double clicks, busy session tracking, and cancellation between delivery segments.
- [x] Assign file cleanup and success recording to the same operation owner.

Readiness: 12 photos result in two groups; partial failure does not trigger re-delivery of already delivered elements; temporary files are not deleted during active use.

### Stage 3: Buttons and Descriptions for All Tracks

- [x] Add text and two buttons in `messages.py` when a description is available.
- [x] Limit the card title length while retaining the complete original description separately.
- [x] Retain existing callbacks and explicitly map new actions to `direct_video`.
- [x] Pass the same text through cache, URL delivery, and file upload.
- [x] Add a single caption from the first media or a full description with separate text.
- [x] Handle description failures without resending media.
- [x] Update the user guide with album rules and long description guidelines.
Readiness: Choosing with or without a description yields the same media, including cache hits; the only difference is the availability of the publication's text.

### Stage 4: Mixed carousels with Instagram

- [x] Retrieve a complete, ordered composition of photos and videos from a single publication.
- [x] Do not take only the first entry for a mixed carousel.
- [x] Integrate existing video preparation and sizing validation.
- [x] Collect mixed albums without replacing videos with previews.
- [x] Verify incomplete responses and backup sources, including the cover-only response.
Readiness: The order and number of items match the original post; unavailable items are not silently removed. Support for stories and highlights is not expanded at this stage.

### Stage 5 – Acceptance and Documentation

- [x] Execute a local matrix, including pytest and ruff.
- [ ] In the user-specified test chat, verify the real Telegram Bot API.
- [ ] Confirm true local Bot API and normal file uploads in available environments.
- [ ] Test display on at least mobile and desktop Telegram clients.
- [x] Record local results: links, number of elements, text length, delivery path, and restrictions.
- [x] Update this document: separate completed items from remaining restrictions.

## 7. Admission Checklist

Notes in the sections below refer to local testing and code verification. The final section requires a live Telegram connection and remains unfilled until external reception occurs.

### Albums and Composition

- [x] 0, 1, 2, 10, 11, 12, 20, and 21 frames produce the expected result using the pure partition function.
- [x] Image order is preserved by file names and element types in local tests.
- [x] Audio is served separately from photos via the direct URL path.
- [x] When a known photo format failure occurs, the document fallback maintains its position in the queue.
- [ ] Cloud upload and local album tracks are accepted by the real Telegram Bot API.
- [x] If the URL is rejected after the first album, the file path sends only the unconfirmed remainder.
- [x] Mixed carousels store all photos and videos, including those at the boundary between two albums.

### Description and Cache

- [x] Verified for empty description, unavailable text, synthetic title, and HTML snippet.
- [x] Tested lengths: 1023, 1024, 1025, 4095, 4096, and 4097, as well as long text with multiple parts.
- [x] Long text in the `title` does not clutter the menu; the source description text is stored separately.
- [x] Cyrillic, emoji, line breaks, markup symbols, links, and long lines are preserved without loss.
- [x] The resulting text is reconstructed from parts without omissions or repetitions.
- [x] The description appears only on the first media or in a single set of text messages for the entire post.
- [x] Locally validated SQLite roundtrip for key `direct_video`, and re-sent one `file_id` with both variants of the description.
- [x] The same `file_id` is accepted for both description variants; the description is not written to the media cache.
- [x] Undescribed selections use already-disassembled metadata and do not trigger a text query.
- [ ] The actual long description is verified against the original publication’s disclosed description.

### Errors, Cancellations, and Competition

- [x] After a confirmed URL rejection in the first or a later group, the file path sends only the remaining items.
- [x] An ambiguous timeout does not trigger an automatic resend.
- [x] `RetryAfter` is processed with limited pause and cancellation.
- [x] Text or audio failure does not result in albums being resent.
- [x] Cancellation before the first block or after the first album stops further sending.
- [x] Double-clicking two different buttons in the same session triggers only one operation.
- [x] Two references from the same user do not mix text, files, and status.
- [x] Creating new sessions does not override active sending by URL with an empty directory.
- [x] Deleting a status message does not interfere with delivery and does not turn it into an error.
- [x] Deleting media to which the text should refer does not cause the media to be redirected.
- [x] Temporary files are closed and cleared after completion of work with them, including in case of partial failure.
- [x] Analytics records one media delivery per request, not one per frame.
- [x] The existing buttons and handlers for YouTube, Rutube, VK, and "Audio Only" pass the shared regression suite.

### Live Telegram acceptance

- [ ] Albums with 2 and 10 items are accepted by the real Bot API; a post with 12 items is displayed by two groups.
- [ ] The caption of the first item is displayed as the album description.
- [ ] Long text is fully read and copied next to the video or album.
- [ ] Replaying a video from the cache returns the selected caption.
- [ ] Verified sending URL from real CDN Instagram and TikTok, with file backup.
- [ ] On mobile and desktop clients, media order, photo, video playback, and description linkage are verified.

The function is considered accepted after performing the relevant items and resolving the real constraints. Local serialization checks and existing tests do not replace the final group of checks.

## 8. Review of the implementation from 2026-09-27

The review was conducted on the working copy after the first implementation. The following components were checked: callback handlers, URL sending, file upload, SQLite cache, Instagram composition analysis, file lifecycle, cancellation, and description linkage with media. Found errors were corrected in the same working copy. The checks from section 7 regarding the real Telegram remain open.

### Found errors and corrections

| Priority | Reproduction and aftermath | Corrected |
|---|---|---|
| P1 | The first album was delivered; the next received an explicit rejection. Returning to the menu and repeating the action discarded progress and restarted the URL path from the first frame. A failure inside the frame fallback preserved the counter but lost the link to the first message. | The counter and the selected action are saved. Repeat continues file sending from the remainder; the first confirmation is saved immediately after each frame. A description mode after partial sending is blocked. The number of elements is checked before continuing. |
| P1 | The normalized list of items was saved only for mixed Instagram posts. A usual photo carousel with multiple yt-dlp entries was erroneously considered incomplete. Later detection of a mixed post in the file path could save only photos. | The file loader first normalizes the composition and selects the path for all photos and videos. The processor accepts refined metadata from the loader's result. |
| P1 | The HTML fallback could return a single cover instead of the full carousel. An element without a URL or a non-verbal sidecar element could be omitted during completeness checks. | The HTML cover does not confirm the full content of the post. The URL of each element and the original number of nodes are verified. If the content is incomplete, submission is stopped before loading. |
| P2 | Any `BadRequest` error when rendering a social video from the cache deleted the `file_id` and triggered a new download, including caption and access errors. | Retry is allowed for known errors of an invalid `file_id`. Other errors do not delete the record or trigger fallback. The action key is also checked for platform compatibility. |
| P2 | A status update error after confirmed delivery could be routed into the media network error handler. An individual audio timeout did not return a structured unknown result. | Service status errors are logged separately and do not affect the media result. For a timeout, audio returns `unknown` with confirmed photos. The normal download button does not retry the request with an unknown result. |
| P2 | A separate audio button could initiate a second operation in the same busy session, but it was not checked before the first request from the cache. | The cancellation button is saved at both the photo and audio stages. A busy session accepts only cancellation; the status of the other link remains independent. Cancellation is checked before each Telegram request, the in-flight request is taken into account, and a single video confirmation is saved. |
| P2 | In a Telegram group, the repeat `reply_text` without an explicit purpose by default quotes the original status message. If the original message was deleted, the text was rejected again. The `InputMedia` constructors load all open files of the group into memory. | Media and backup text use `do_quote=False`; the chat topic is preserved. Groups use `InputFile(read_file_handle=False)` and keep files open until the end of the request, then close them. |

The main fixes are in [telegram_utils.py](../../../utils/telegram_utils.py), [tiktok_instagram_utils.py](../../../utils/tiktok_instagram_utils.py), and [platform_actions.py](../../../utils/platform_actions.py).
The `do_quote=False` behavior and lifetime of streamed `InputFile` objects were checked against the [Message](https://docs.python-telegram-bot.org/en/stable/telegram.message.html) and [InputFile](https://docs.python-telegram-bot.org/en/stable/telegram.inputfile.html) documentation. A substituted network transport also verified that a repeated description stays in the same chat and topic without quoting a deleted status message.

### Checking After Corrections

`.venv/bin/coverage run --branch -m pytest tests/`: **824 passed**
`coverage report --fail-under=70`: **72%** - threshold passed
`ruff check .` and `git diff --check` passed without findings.

- [x] Added 21 regression checks to [test_social_delivery_review.py](../../../tests/test_social_delivery_review.py)
- [x] Checked remainder repetition, caption preservation, first confirmation with partial fallback, and prohibition of competing actions
- [x] Checked photo carousel, missing frame, invalid node, HTML cover, and later-detected mixed post
- [x] Checked caption-related `BadRequest` on cached video, status update failures, and unknown delivery outcomes for separate audio
- [x] Checked cancellation before network call, availability of cancel button, file streams, and closure after request
- [x] Checked saving chat/topic when repeating text without deleted media

---

## Acceptance Limitations

The following are still not implemented:
- Real requests to the cloud and local Bot API
- Display in mobile and desktop Telegram
- Long recipe comparison with the original post

These are open points in section 7.
Carousels with only a few videos did not expand during this review: when the full composition cannot be retrieved, the processor stops with a message indicating an incomplete carousel.

---

## 9. Refinement of the Error Classifier (Updated: 2026-09-27)

After the review, the user allowed the code to be corrected and separately requested the removal of unknown error classes. The following components were reviewed:
- User code generators
- `USER_FLOW_FAIL` journal
- Polling
- Canary flows
- yt-dlp downloaders
- File senders

### Error Classification Updates

- `UNKNOWN` is no longer created as a new error category.
- Known error types and messages are now assigned a root cause from the following categories:
  `RATE_LIMIT`, `ACCESS`, `NETWORK`, `TIMEOUT`, `API`, `RENDER`, `LARGE`, `FILE`, `COOKIE`, `DOWNLOAD`, `DATA`, `FFMPEG`, `ROUTE`, and others (see [code directory](../../error-codes.md)).
- If neither the error text nor the exception type can be confirmed as a cause, `UNEXPECT` is used with the exception type, and this is logged in the administrative log.

### Updated Error Handling

- The sending code and log entry now use the same error classifier, including for:
  - `TimedOut`
  - `BadRequest`
  - Missing file
  - Access errors
- Asynchronous task cancellation errors are no longer reported to the user as network timeouts.
- HTTP client errors are categorized by both type and response status.
- The undefined `DownloadError` remains classified as a loader error.
- Neutral user text for `UNEXPECT` does not attribute failure to authorization or post deletion.
- The `unknown` status in Telegram delivery is preserved: it means no delivery confirmation, not a category error, and automatic repetition is still prohibited.

---

### Checking the Classifier

- [x] Checked all `UNKNOWN` creation sites in user code, polling, canary, and yt-dlp; no new error codes are created with this category.
- [x] Verified PTB types (`RetryAfter`, `TimedOut`, `BadRequest`, `Forbidden`), the HTTP client, the file system, and loader errors.
- [x] The error code matches the full category in the log, including the eight-character abbreviation.
- [x] Independently verified missing and oversized canary files, missing file when sent, `BadRequest` error failures, incorrect format, and missing audio track.
- [x] Full local run: `.venv/bin/coverage run --branch -m pytest tests/` - **863 passed**; `coverage report --fail-under=70` - **72%**; `ruff check .` and `git diff --check` with no further issues.
- [ ] Verify new messages and error codes on the Bot API simultaneously with the actual receiving points from Section 7.
This is a local verification of Telegram's code and simulations. The behavior in actual chats and client displays remains unverified.
