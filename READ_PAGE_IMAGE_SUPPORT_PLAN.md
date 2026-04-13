Most appropriate approach: keep `read_page` as a two-step asset reader.

1. `read_page` first lists image metadata.
2. `read_page` then fetches selected image(s) only when explicitly asked.

That keeps normal calls cheap and gives the LLM image access on demand.

**Why this is the right shape**

- `search_and_crawl` should stay lean.
- `read_page` already owns page-adjacent detail.
- You do not want to inline image bytes/base64 in normal responses.
- The model usually only needs pixels for 1-2 images, not every image on the page.

**Important current limitation**

Right now you only store up to `5` images at crawl time via `_normalize_images(..., limit=5)`, so “list all images” is not possible yet.

First change should be:

- store more image metadata per page, separate from how much you return by default
- example:
  - store up to `50` filtered image metadata records
  - return only a small number by default

**Recommended API shape**

Extend `read_page` with a small image sub-mode.

Suggested new params:

- `image_list_limit: int = 5`
- `image_offset: int = 0`
- `selected_image_ids: list[str] = []`
- `image_detail: Literal["low", "high"] = "low"`
- `include_page_content: bool = True`

Suggested behavior:

- normal read:
  - returns page content
  - returns image metadata list only
- image-on-demand read:
  - caller passes `selected_image_ids`
  - tool fetches only those images
  - optionally omit page content with `include_page_content=False`

**Use stable image IDs, not raw URLs**

Do not make the LLM pass long image URLs back.

Return image metadata like:

```json
{
  "image_id": "218d19b99f:3",
  "url": "https://react.dev/...",
  "alt": "Diagram of effect lifecycle"
}
```

Then the LLM can ask for:

```json
{
  "page_ids": ["218d19b99f"],
  "selected_image_ids": ["218d19b99f:3"],
  "image_detail": "low",
  "include_page_content": false
}
```

**What `read_page` should return**

Default listing mode:

```json
{
  "page_id": "...",
  "content": "...",
  "images_total": 12,
  "images_more_available": true,
  "images": [
    {"image_id": "...:1", "url": "...", "alt": "..."},
    {"image_id": "...:2", "url": "...", "alt": "..."}
  ]
}
```

On-demand image fetch mode:

```json
{
  "page_id": "...",
  "selected_images": [
    {
      "image_id": "...:2",
      "url": "...",
      "alt": "...",
      "mime_type": "image/jpeg",
      "width": 1200,
      "height": 800,
      "view_url": "http://.../cached-image/abc"
    }
  ]
}
```

**How the model actually “sees” the image**

This is the key design choice.

Best option:
- if your MCP/OpenWebUI path can return actual image content/attachments, use that

Fallback option:
- return a cached local/proxied `view_url`
- optionally also return a tool-generated text description if the host cannot pass pixels into the model

I would not put base64 image blobs in normal JSON responses unless absolutely necessary.

**Implementation plan**

1. Crawl/storage layer
- change `_normalize_images` to collect more metadata
- store `image_id`, `url`, `alt`
- optionally keep `width`/`height` if available
- keep a stored cap like `50`

2. Read API
- add `image_list_limit`, `image_offset`, `selected_image_ids`, `image_detail`, `include_page_content`
- return `images_total` and `images_more_available`

3. On-demand fetch layer
- fetch selected image URLs only when requested
- cache them by hashed URL
- resize/compress for `low` detail
- keep `high` only when requested

4. Transport layer
- if host supports image attachments, return them
- otherwise return `view_url`
- optional fallback: OCR/caption summary

5. Safety/limits
- max `selected_image_ids` per call: `1-3`
- content-type must be `image/*`
- size cap
- timeout cap
- block `data:` URLs and weird schemes
- probably avoid raw SVG fetch/render at first

6. Quality filters
- filter tiny icons/logos/avatars if possible
- preserve meaningful content images
- keep current metadata-only images in normal results

**Recommended rollout**

Phase 1:
- store more image metadata
- add image listing/pagination to `read_page`

Phase 2:
- add `selected_image_ids` and cached image fetch

Phase 3:
- add optional vision/OCR summary fallback if host cannot directly pass images

**My recommendation**

Use `read_page` for both:
- listing image metadata
- fetching selected image pixels

But make image fetching an explicit sub-mode, not default behavior.

If you want, I can turn this into an exact proposed `read_page` schema and response contract next.
