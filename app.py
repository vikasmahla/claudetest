import os
import json
import csv
import io
from flask import Flask, request, jsonify, send_from_directory
import anthropic

app = Flask(__name__, static_folder="static")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are the Founder of Siba Consulting, an ad creative agency writing cold emails.

Your job: given a product/company, research their customer reviews (from their website and Reddit) and generate a cold email personalization.

Rules:
- Search for real customer reviews or Reddit discussions about the product
- Identify POSITIVE reviews that show initial skepticism, OR surprising/niche things people love (not obvious things like taste or appearance)
- If no reviews found or only negative reviews, find a unique feature/benefit/problem the product solves from its website
- Do NOT pick obvious things (how good it tastes, looks, smells)
- Keep everything SHORT and punchy — max 12-15 words for observation and suggestion combined each
- Observation must start with "I noticed" or "I saw"
- Suggestion must start with "Have you thought about" or "Have you considered"
- Subject must follow pattern: "ad creative idea for [niche angle] (on us)"

Output ONLY valid JSON with these exact keys:
{
  "subject": "...",
  "observation": "...",
  "suggestion": "..."
}"""


def generate_for_product(company: str, website: str) -> dict:
    """Use Claude with web search to generate cold email personalization."""
    user_message = f"""Research this company and generate cold email personalization:

Company: {company}
Website: {website}

First visit their website to understand what they sell. Then search Reddit and review sites for customer discussions.
Find a niche, non-obvious angle — a skepticism customers had before buying, or a surprising thing people love.
Then output the JSON."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
