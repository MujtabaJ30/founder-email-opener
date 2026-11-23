"""Browser interface for the founder email opener."""

from ipaddress import ip_address
import os
import socket
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from openai import OpenAIError

from opener import draft_opener, model_client
from retrieve import collect, website_url


app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4096


@app.get("/")
def index():
    repository_url = os.environ.get("OPENER_REPOSITORY_URL", "").strip()
    return render_template(
        "index.html",
        repository_url=repository_url if repository_url.startswith("https://github.com/") else "",
        example_website=os.environ.get("OPENER_EXAMPLE_WEBSITE", "").strip(),
    )


def public_website(value: str) -> str:
    url = website_url(value)
    hostname = urlsplit(url).hostname
    if not hostname or "." not in hostname:
        raise ValueError("Enter a public company website, such as company.com.")
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("We couldn't find that website. Check the address and try again.") from error
    if not addresses or any(not ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("Enter a public company website.")
    return url


@app.post("/api/opener")
def generate():
    body = request.get_json(silent=True)
    value = body.get("website") if isinstance(body, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return jsonify(message="Enter a company website, such as company.com."), 400
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return jsonify(message="The generator is unavailable right now."), 503
    try:
        url = public_website(value)
    except ValueError as error:
        return jsonify(message=str(error)), 400

    site = collect(url, max_pages=7, timeout=20)
    try:
        with model_client(api_key) as client:
            result = draft_opener(site, client)
    except OpenAIError as error:
        app.logger.warning("Model request failed: %s", type(error).__name__)
        return jsonify(message="The generator couldn't complete this draft. Try again in a moment."), 502
    if result["status"] == "insufficient_evidence":
        return jsonify(message="We couldn't find enough evidence for a useful opener. Try the company's homepage or a public product page."), 422
    if result["status"] != "draft":
        return jsonify(message="A draft was withheld because its source evidence couldn't be verified. Try again."), 422

    page_titles = {page["url"]: page["title"] for page in site["pages"]}
    source_pages = {}
    for item in result["evidence"]:
        source_url = item["url"]
        source = source_pages.setdefault(source_url, {
            "url": source_url,
            "title": page_titles.get(source_url) or urlsplit(source_url).hostname,
            "quotes": [],
        })
        if item["quote"] not in source["quotes"]:
            source["quotes"].append(item["quote"])
    response = jsonify(lines=result["lines"], evidence=list(source_pages.values()))
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, use_reloader=False)
