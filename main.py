#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
# ]
# ///

import argparse
import html
import json
import re
import subprocess
import time
from datetime import date as date_type
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

import requests


FEED_URL = "https://denikn.cz/newsletter/rannich-5-minut/feed/"
POLL_INTERVAL_SECONDS = 60 * 5

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def http_get(url, timeout=20):
    response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


class DateNotAvailableError(RuntimeError):
    pass


def fetch_latest_overview_url(target_date=None):
    if target_date is None:
        target_date = date_type.today()
    try:
        response = http_get(FEED_URL)
    except Exception as exc:
        raise RuntimeError("Could not fetch the RSS feed.") from exc

    url = parse_rss_for_latest_link(response.text, target_date=target_date)
    if url:
        return url
    raise DateNotAvailableError(
        f"No RSS entry found for date {target_date.isoformat()}."
    )


def parse_rss_for_latest_link(xml_text, target_date):
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)
    except Exception:
        return None

    channel = root.find("channel")
    if channel is None:
        channel = root.find("{*}channel")
    if channel is None:
        return None

    for item in channel.findall("item") + channel.findall("{*}item"):
        link = item.findtext("link") or item.findtext("{*}link")
        if not link:
            continue
        item_date = parse_rss_item_date(item)
        if not item_date or item_date != target_date:
            continue
        return link.strip()
    return None


def parse_rss_item_date(item):
    date_text = (
        item.findtext("pubDate")
        or item.findtext("{*}pubDate")
        or item.findtext("date")
        or item.findtext("{*}date")
        or item.findtext("published")
        or item.findtext("{*}published")
    )
    if not date_text:
        return None
    date_text = date_text.strip()
    if not date_text:
        return None
    try:
        return datetime.fromisoformat(date_text).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(date_text).date()
    except Exception:
        pass
    match = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
    if match:
        try:
            return date_type.fromisoformat(match.group(0))
        except ValueError:
            return None
    return None


def extract_json_ld_article(html_text):
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for script in scripts:
        script = script.strip()
        if not script:
            continue
        try:
            data = json.loads(script)
        except json.JSONDecodeError:
            continue
        for item in ensure_list(data):
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and not any(
                t in ("NewsArticle", "Article") for t in types
            ):
                continue
            body = item.get("articleBody") or item.get("description")
            title = item.get("headline") or item.get("name")
            date = item.get("datePublished") or item.get("dateModified")
            if body or title:
                return {"title": title, "date": date, "body": body}
    return None


def ensure_list(value):
    if isinstance(value, list):
        return value
    return [value]


class ArticleTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture_stack = []
        self.ignore_depth = 0
        self.lines = []
        self.current = ""
        self.pending_prefix = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth += 1
            return
        if self.ignore_depth:
            return
        if tag in ("article", "main"):
            self.capture_stack.append(tag)
        if not self.capture_stack:
            return
        if tag in ("p", "br", "h1", "h2", "h3", "h4"):
            self.flush_line()
        if tag == "li":
            self.flush_line()
            self.pending_prefix = "- "

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth = max(0, self.ignore_depth - 1)
            return
        if self.ignore_depth:
            return
        if self.capture_stack and tag == self.capture_stack[-1]:
            self.flush_line()
            self.capture_stack.pop()
        if not self.capture_stack:
            return
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "br"):
            self.flush_line()

    def handle_data(self, data):
        if self.ignore_depth or not self.capture_stack:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self.pending_prefix:
            self.current += self.pending_prefix
            self.pending_prefix = ""
        if self.current and not self.current.endswith(" "):
            self.current += " "
        self.current += text

    def flush_line(self):
        line = self.current.strip()
        if line:
            self.lines.append(line)
        self.current = ""
        self.pending_prefix = ""


class NewsletterMinuteExtractor(HTMLParser):
    TARGET_CLASS = "wp-block-dn-newsletter-r5m-minute"

    def __init__(self):
        super().__init__()
        self.capture_depth = 0
        self.ignore_depth = 0
        self.items = []
        self.current = ""
        self.current_lines = []
        self.current_bullets = []
        self.in_bullet = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth += 1
            return
        if self.ignore_depth:
            return
        if self.capture_depth:
            self.capture_depth += 1
        elif tag == "div" and self._has_target_class(attrs):
            self.capture_depth = 1
        else:
            return
        if tag in ("p", "br", "h1", "h2", "h3", "h4", "ul"):
            self.flush_line()
        if tag == "li":
            self.flush_line()
            self.in_bullet = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth = max(0, self.ignore_depth - 1)
            return
        if self.ignore_depth or not self.capture_depth:
            return
        if tag in ("p", "li", "h1", "h2", "h3", "h4", "br", "ul"):
            self.flush_line()
        if tag == "li":
            self.in_bullet = False
        self.capture_depth = max(0, self.capture_depth - 1)
        if self.capture_depth == 0:
            self.flush_item()

    def handle_data(self, data):
        if self.ignore_depth or not self.capture_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self.current and not self.current.endswith(" "):
            self.current += " "
        self.current += text

    def flush_line(self):
        line = self.current.strip()
        if line:
            if self.in_bullet:
                self.current_bullets.append(line)
            else:
                self.current_lines.append(line)
        self.current = ""

    def flush_item(self):
        self.flush_line()
        if self.current_lines or self.current_bullets:
            item_text = "\n".join(self.current_lines).strip()
            self.items.append(
                {"text": item_text, "bullets": list(self.current_bullets)}
            )
        self.current_lines = []
        self.current_bullets = []
        self.in_bullet = False

    def _has_target_class(self, attrs):
        for key, value in attrs:
            if key == "class" and value:
                classes = value.split()
                return self.TARGET_CLASS in classes
        return False


class NewsletterGroupExtractor(HTMLParser):
    TARGET_CLASS = "wp-block-dn-newsletter-r5m-group"

    def __init__(self):
        super().__init__()
        self.capture_depth = 0
        self.ignore_depth = 0
        self.groups = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth += 1
            return
        if self.ignore_depth:
            return
        if self.capture_depth:
            self.capture_depth += 1
            self.current.append(self.get_starttag_text())
            return
        if tag == "div" and self._has_target_class(attrs):
            self.capture_depth = 1
            self.current = [self.get_starttag_text()]

    def handle_startendtag(self, tag, attrs):
        if self.ignore_depth or not self.capture_depth:
            return
        self.current.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self.ignore_depth = max(0, self.ignore_depth - 1)
            return
        if self.ignore_depth or not self.capture_depth:
            return
        self.current.append(f"</{tag}>")
        self.capture_depth = max(0, self.capture_depth - 1)
        if self.capture_depth == 0 and self.current:
            self.groups.append("".join(self.current))
            self.current = []

    def handle_data(self, data):
        if self.ignore_depth or not self.capture_depth:
            return
        self.current.append(data)

    def _has_target_class(self, attrs):
        for key, value in attrs:
            if key == "class" and value:
                classes = value.split()
                return self.TARGET_CLASS in classes
        return False


def extract_newsletter_groups(html_text, limit=None):
    extractor = NewsletterGroupExtractor()
    extractor.feed(html_text)
    groups = extractor.groups
    if limit is None:
        return groups
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer or None")
    if limit <= 0:
        return []
    return groups[:limit]


def extract_newsletter_minutes(html_text):
    groups = extract_newsletter_groups(html_text, limit=1)
    items = []
    if groups:
        for group_html in groups:
            extractor = NewsletterMinuteExtractor()
            extractor.feed(group_html)
            extractor.flush_item()
            items.extend(extractor.items)

    extractor = NewsletterMinuteExtractor()
    extractor.feed(html_text)
    extractor.flush_item()
    all_items = extractor.items

    items = [item for item in items if item.get("text") or item.get("bullets")]
    items.extend([item for item in all_items if "Počasí" in item.get("text")])
    return items


def extract_article_text(html_text):
    minutes = extract_newsletter_minutes(html_text)
    if minutes:
        return minutes
    extractor = ArticleTextExtractor()
    extractor.feed(html_text)
    extractor.flush_line()
    text = "\n".join(extractor.lines).strip()
    if text:
        return text
    return fallback_strip_html(html_text)


def fallback_strip_html(html_text):
    normalized = re.sub(
        r"</(p|li|h1|h2|h3|h4|br)>",
        "\n",
        html_text,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"<br\\s*/?>", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = html.unescape(normalized)
    normalized = re.sub(r"[ \\t\\r]+", " ", normalized)
    lines = [line.strip() for line in normalized.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def extract_title(html_text):
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    title = re.sub(r"\s+[-|–]\s+.*$", "", title)
    return title.strip()


def fetch_article(url):
    response = http_get(url)
    html_text = response.text
    payload = extract_json_ld_article(html_text) or {}
    title = extract_title(html_text) or "Daily overview"
    date = payload.get("date")
    extracted = extract_article_text(html_text)
    items = []
    body = ""
    if isinstance(extracted, list):
        items = []
        for item in extracted:
            if isinstance(item, dict):
                text = (item.get("text") or "").strip()
                bullets = [b.strip() for b in item.get("bullets") or [] if b.strip()]
                items.append({"text": text, "bullets": bullets})
            else:
                items.append({"text": str(item).strip(), "bullets": []})
        body = "\n\n".join(
            filter(None, [item["text"] for item in items if item.get("text")])
        ).strip()
    else:
        body = extracted or ""
    if not body and not items:
        body = "No article text found."
    return {
        "url": url,
        "title": title,
        "date": date,
        "body": body,
        "items": items,
    }


def escape_typst_text(text):
    replacements = {
        "\\": "\\\\",
        "*": "\\*",
        "_": "\\_",
        "#": "\\#",
        "[": "\\[",
        "]": "\\]",
        "{": "\\{",
        "}": "\\}",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def escape_typst_link_target(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def extract_date_only(date_value):
    if not date_value:
        return None
    if isinstance(date_value, datetime):
        return date_value.date().isoformat()
    if isinstance(date_value, date_type):
        return date_value.isoformat()
    if isinstance(date_value, str):
        text = date_value.strip()
        if not text:
            return None
        match = re.search(r"(\\d{4}-\\d{2}-\\d{2})", text)
        if match:
            return match.group(1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.date().isoformat()
    return None


def czech_weekday(date_value):
    if not date_value:
        return None
    if isinstance(date_value, (datetime, date_type)):
        day_index = date_value.weekday()
    elif isinstance(date_value, str):
        text = date_value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            match = re.search(r"(\\d{4}-\\d{2}-\\d{2})", text)
            if not match:
                return None
            try:
                parsed = datetime.fromisoformat(match.group(1))
            except ValueError:
                return None
        day_index = parsed.weekday()
    else:
        return None
    names = [
        "pondělí",
        "úterý",
        "středa",
        "čtvrtek",
        "pátek",
        "sobota",
        "neděle",
    ]
    return names[day_index]


def format_typst(article):
    title = article.get("title") or "Daily overview"
    date = article.get("date")
    url = article.get("url")
    items = article.get("items") or []
    lines = []

    lines.append(
        """
#set page(
  paper: "a4",
  columns: 2,
  margin: 1cm,
  footer: context [
"""
        f"  *Ranních 5 minut -- {escape_typst_text(czech_weekday(date))} -- {escape_typst_text(date)}*"
        """
    #h(1fr)
    #counter(page).display(
      "1/1",
      both: true,
    )
  ]
)

#set columns(gutter: 12pt)
#set text(
  font: "Franklin Gothic FS",
  size: 10pt,
)

#let separator(
  width: 60%,
  stroke: 0.5pt,
  top-gap: 0.1em,
  bottom-gap: 0.1em,
) = {
  v(top-gap)
  align(center)[
    #line(length: width, stroke: stroke)
  ]
  v(bottom-gap)
}
"""
    )

    lines.append(f"= {escape_typst_text(title)}")
    if date:
        lines.append(
            f"_Vydáno: {escape_typst_text(date)}, {escape_typst_text(czech_weekday(date))}_"
        )
    lines.append("")

    if items:
        for index, item in enumerate(items):
            heading = (item.get("text") or "").strip() or "Item"
            lines.append(f"{escape_typst_text(heading)}")
            bullets = [b.strip() for b in item.get("bullets") or [] if b.strip()]
            for bullet in bullets:
                lines.append(f"- {escape_typst_text(bullet)}")
            if index < len(items) - 1:
                lines.append("")
            lines.append("#separator()")
        lines.append(f'_Zdroj: #link("{escape_typst_link_target(url)}")_')
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    body = article.get("body", "") or ""
    for line in body.splitlines():
        lines.append(escape_typst_text(line))
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "-n",
            "--dry",
            action="store_true",
            help="Skip printing the generated PDF.",
        )
        parser.add_argument(
            "-P",
            "--printer",
            default=None,
            help="Printer name passed to lpr (optional).",
        )
        parser.add_argument(
            "-d",
            "--date",
            default=None,
            help="ISO date (YYYY-MM-DD) to fetch (defaults to today).",
        )
        parser.add_argument(
            "--poll",
            action="store_true",
            help=(
                "Poll the RSS feed until the requested date appears "
                "(use with today's date)."
            ),
        )
        args = parser.parse_args()
        if args.date:
            try:
                target_date = date_type.fromisoformat(args.date)
            except ValueError as exc:
                raise RuntimeError("Date must be in ISO format YYYY-MM-DD.") from exc
        else:
            target_date = date_type.today()
        while True:
            try:
                overview_url = fetch_latest_overview_url(target_date=target_date)
                break
            except DateNotAvailableError:
                if not args.poll:
                    raise
                print(
                    f"Date {target_date.isoformat()} not yet available; "
                    f"retrying in {POLL_INTERVAL_SECONDS}s..."
                )
                time.sleep(POLL_INTERVAL_SECONDS)
        article = fetch_article(overview_url)
        print(f"Overview for {target_date.isoformat()}:")
        print(article["date"])
        print(article["title"])
        print(article["url"])
        print()
        date_only = extract_date_only(article.get("date")) or "unknown-date"
        output_path = f"rannich-5minut-{date_only}.typ"
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(format_typst(article))
        print(f"Typst file written: {output_path}")
        subprocess.run(["typst", "compile", output_path], check=True)
        pdf_path = (
            f"{output_path[:-4]}.pdf"
            if output_path.lower().endswith(".typ")
            else f"{output_path}.pdf"
        )
        if not args.dry:
            lpr_command = ["lpr"]
            if args.printer:
                lpr_command.extend(["-P", args.printer])
            lpr_command.extend(
                [
                    "-o",
                    "sides=two-sided-long-edge",
                    "-o",
                    "media=iso_a4_210x297mm",
                    pdf_path,
                ]
            )
            subprocess.run(lpr_command, check=True)
    except Exception as e:
        print(f"Error exporting overview: {e}")
