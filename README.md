# Founder-email opener

A local tool that drafts a three-line email opener from a company website. Set `OPENAI_API_KEY` in your shell before running. Python 3.11 was used for verification.

```shell
python -m pip install -r requirements.txt
python opener.py company.com
```

Replace `company.com` with the company’s real website. The command prints three lines when it can ground a draft in the website. Use `--json` to review source links, exact excerpts, the proposed action, page-read errors and token usage. The default is GPT-6.1 Terra with low reasoning. The program makes one Responses API call when it has readable evidence.

To inspect what the reader finds without a model call, run `python retrieve.py https://example.com/ --max-pages 7 --timeout 20`. It returns public HTML and Markdown text, URLs, request failures and elapsed time. The reader follows relevant same-host links and can use a sitemap to replace a weaker last page with a customer workflow guide.

Each draft is for human review. A matching excerpt confirms the quoted text was supplied to the model. Review what the draft infers from it before using the opener. This CLI is intended for trusted company URLs and local use.

To open the browser version locally, run `python app.py` and visit `http://127.0.0.1:8765/`. Enter a company website and select **Generate opener**. The page shows an in-progress state, then the three-line draft, a copy button, and the exact cited pages with their excerpts. The local server listens on the loopback address.

### Optional configuration

- `OPENER_PROMPT_FILE` points to a local UTF-8 prompt file for sender-specific drafting instructions. The app uses a general-purpose prompt by default. Keep sender details that are private in a local file outside the repository.
- `OPENER_EXAMPLE_WEBSITE` adds a **Try an example** shortcut that fills in the configured public website address.
- `OPENER_REPOSITORY_URL` adds a **View source on GitHub** link when set to the full GitHub repository URL.
