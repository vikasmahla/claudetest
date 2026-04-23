import os
import json
import re
import csv
import io
import requests as http_requests
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__, static_folder="static")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are an AI-powered web researcher for Siba Consulting, an ad creative agency for ecommerce brands. Your task is to read online customer reviews from the company's own website and from Reddit to inspire a unique ad creative idea. If you cannot find customer reviews or if available reviews are only negative (product flaws), use the company website to identify a unique product feature, benefit, or problem it solves, and craft an idea from that observation. You must return concise fields that will be used in a cold email: a short subject, a one-line observation grounded in reviews or site content, and a one-line suggestion for a video concept.

OBJECTIVE:
Extract one niche, non-obvious insight from online reviews (or from the company website if reviews are missing/only negative) and generate:
- a short subject that begins with "ad creative idea for …"
- a one-line observation that starts with "I noticed" or "I saw"
- a one-line suggestion that starts with "Have you thought" or "Have you considered about a video"
All lines must be simple, to the point, and keep observation/suggestion to 12–15 words.

INSTRUCTIONS:
1. Source discovery (limit to a single web search):
   - Perform at most one web search to find either the company's reviews page(s) on its website or relevant Reddit threads mentioning the brand/product. Prefer first-party review pages, product pages with reviews, or Reddit posts/comments that discuss real usage.

2. Review extraction:
   - From the located page(s), extract several short positive reviews or comments that reveal skepticism before trying, common objections, or quirky specifics people like (e.g., a material, ingredient, mechanism). Avoid obvious themes like "tastes good" or "looks good."
   - If you cannot find reviews or only find negative defect-focused reviews, switch to the company website's product pages and identify a unique feature/benefit/problem-solved to base the idea on.

3. Synthesis rules:
   - Identify one niche topic suitable for a video that addresses skepticism, objections, or a quirky liked element.
   - Keep it non-obvious and specific; avoid generic praise like taste/looks.
   - Keep language concise and direct; avoid flowery wording.

4. Output formatting constraints:
   - subject: Begin with "ad creative idea for…"; keep it short (ideally under 7 words if possible).
   - observation: Start with "I noticed …" or "I saw …"; 12–15 words maximum.
   - suggestion: Start with "Have you thought …" or "Have you considered about a video …"; 12–15 words maximum.
   - Do not include links or citations in the output fields.

5. Fallback logic:
   - If reviews are unavailable or only negative, clearly base the observation on a unique product aspect from the company website.

6. Quality checks:
   - Ensure the niche topic is not about taste or looks.
   - Ensure each line is within word limits and follows required starter phrases.

Output ONLY valid JSON with these exact keys:
{
  "subject": "...",
  "observation": "...",
  "suggestion": "..."
}"""


def generate_for_product(company: str, website: str) -> dict:
    """Use Claude with web search to generate cold email personalization."""
    user_message = f"""Company Website: {website}
Company Name: {company}

Follow the instructions in the system prompt. Perform at most one web search, extract a niche non-obvious insight from reviews or the website, then output the JSON."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 1,
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract text from response
    result_text = ""
    for block in response.content:
        if block.type == "text":
            result_text = block.text
            break

    # Parse JSON from response
    start = result_text.find("{")
    end = result_text.rfind("}") + 1
    if start != -1 and end > start:
        return json.loads(result_text[start:end])

    # Fallback if JSON not found
    return {
        "subject": f"ad creative idea for {company} (on us)",
        "observation": "Could not extract structured response.",
        "suggestion": "Please try again.",
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/generate", methods=["POST"])
def generate():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    stream = io.StringIO(file.stream.read().decode("utf-8"))
    reader = csv.DictReader(stream)
    rows = list(reader)

    if not rows:
        return jsonify({"error": "CSV is empty"}), 400

    fieldnames = reader.fieldnames or []

    def find_col(options):
        for opt in options:
            for h in fieldnames:
                if h.strip().lower() == opt:
                    return h
        return None

    col_company = find_col(["company", "company name", "brand", "name"])
    col_website = find_col(["website", "url", "site", "link", "company website"])

    results = []
    for row in rows:
        company = row.get(col_company, "").strip() if col_company else ""
        website = row.get(col_website, "").strip() if col_website else ""

        if not company:
            continue

        try:
            data = generate_for_product(company, website)
            results.append(
                {
                    "company": company,
                    "website": website,
                    "subject": data.get("subject", ""),
                    "observation": data.get("observation", ""),
                    "suggestion": data.get("suggestion", ""),
                    "error": None,
                }
            )
        except Exception as e:
            results.append(
                {
                    "company": company,
                    "website": website,
                    "subject": "",
                    "observation": "",
                    "suggestion": "",
                    "error": str(e),
                }
            )

    return jsonify({"results": results})


@app.route("/generate-single", methods=["POST"])
def generate_single():
    body = request.get_json()
    company = body.get("company", "").strip()
    website = body.get("website", "").strip()

    if not company:
        return jsonify({"error": "Company name is required"}), 400

    try:
        data = generate_for_product(company, website)
        return jsonify(
            {
                "company": company,
                "website": website,
                "subject": data.get("subject", ""),
                "observation": data.get("observation", ""),
                "suggestion": data.get("suggestion", ""),
                "error": None,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Meta Ad Library ──────────────────────────────────────────────────────────

META_GRAPH = "https://graph.facebook.com/v21.0"
_FB_SKIP_PATHS = {"pages", "groups", "events", "marketplace", "watch", "gaming", "ads", "business", "help"}


def _parse_fb_url(url):
    """Return ('id'|'name', value) for a Facebook URL, or (None, None)."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "facebook.com" not in host and "fb.com" not in host:
        return None, None
    qs = parse_qs(parsed.query)
    if "id" in qs:
        return "id", qs["id"][0]
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if parts and parts[0].lower() not in _FB_SKIP_PATHS:
        return "name", parts[0]
    return None, None


def _domain_to_term(url):
    """Extract a bare company name from a website URL."""
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    host = re.sub(r"^www\.", "", host.lower())
    return host.split(".")[0]


def _lookup_page(identifier, token):
    """Return (page_dict, error_str)."""
    resp = http_requests.get(
        f"{META_GRAPH}/{identifier}",
        params={"fields": "id,name,fan_count,category,verification_status", "access_token": token},
        timeout=15,
    )
    data = resp.json()
    if "error" in data:
        return None, data["error"].get("message", "Page lookup failed")
    return data, None


def _fetch_ad_library(token, country, active_status, page_id=None, search_term=None):
    """Paginate the Ad Library and return aggregated stats."""
    params = {
        "ad_reached_countries": json.dumps([country]),
        "ad_active_status": active_status,
        "fields": "id,ad_creation_time,ad_creative_bodies,ad_creative_link_titles,"
                  "ad_delivery_start_time,ad_delivery_stop_time,page_id,page_name,"
                  "publisher_platforms,impressions,spend,ad_snapshot_url",
        "limit": 100,
        "access_token": token,
    }
    if page_id:
        params["search_page_ids"] = page_id
    elif search_term:
        params["search_terms"] = search_term
    else:
        return None, "No search criteria provided"

    all_ads, page_count, next_url, cap = [], 0, f"{META_GRAPH}/ads_archive", 5
    while next_url and page_count < cap:
        resp = http_requests.get(next_url, params=params if page_count == 0 else None, timeout=20)
        data = resp.json()
        if "error" in data:
            if not all_ads:
                return None, data["error"].get("message", "Ad Library API error")
            break
        all_ads.extend(data.get("data", []))
        page_count += 1
        next_url = data.get("paging", {}).get("next")

    active = [a for a in all_ads if not a.get("ad_delivery_stop_time")]
    platforms = sorted({p for a in all_ads for p in a.get("publisher_platforms", [])})
    return {
        "total": len(all_ads),
        "capped": page_count >= cap and bool(next_url),
        "active": len(active),
        "inactive": len(all_ads) - len(active),
        "platforms": platforms,
        "sample_ads": all_ads[:8],
    }, None


@app.route("/check-meta-ads", methods=["POST"])
def check_meta_ads():
    body = request.get_json() or {}
    url_input = body.get("url", "").strip()
    token = body.get("access_token", "").strip() or os.environ.get("META_ACCESS_TOKEN", "")
    country = body.get("country", "US")
    active_status = body.get("active_status", "ALL")

    if not url_input:
        return jsonify({"error": "URL is required"}), 400
    if not token:
        return jsonify({"error": "Meta access token required"}), 400

    if not url_input.startswith("http"):
        url_input = "https://" + url_input

    result = {"input": url_input}
    page_id, search_term = None, None

    id_type, value = _parse_fb_url(url_input)
    if id_type and value:
        page_data, err = _lookup_page(value, token)
        if page_data:
            result["page"] = {
                "id": page_data.get("id"),
                "name": page_data.get("name"),
                "fans": page_data.get("fan_count"),
                "category": page_data.get("category"),
                "verified": page_data.get("verification_status") == "blue_verified",
            }
            page_id = page_data["id"]
        else:
            result["page_error"] = err
            search_term = value
    else:
        search_term = _domain_to_term(url_input)

    result["search_term"] = search_term or value

    ads, err = _fetch_ad_library(
        token, country, active_status,
        page_id=page_id,
        search_term=search_term if not page_id else None,
    )
    if err:
        return jsonify({"error": err}), 400

    result.update(ads)
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
