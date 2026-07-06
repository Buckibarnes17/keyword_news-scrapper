# ## Changes (Trafilatura Integration — KeywordScout v2.0 Upgrade)
# - Added configurable extraction quality thresholds (MIN_EXTRACTED_SIZE and MIN_RETENTION_RATIO).
# - Updated convert_html_to_firecrawl_schema to use Trafilatura for markdown if validation holds.

import re
import urllib.parse
import os
from typing import Dict, Any, List, Set, Tuple
from bs4 import BeautifulSoup, Comment, NavigableString

# Extraction quality thresholds (override via environment variables)
MIN_EXTRACTED_SIZE = int(os.environ.get("KS_MIN_EXTRACTED_SIZE", "200"))   # min chars for valid extraction
MIN_RETENTION_RATIO = float(os.environ.get("KS_MIN_RETENTION_RATIO", "0.95"))  # existing threshold

# Regex patterns that indicate related articles, comments, or other sections that come after the main article
STOP_PATTERNS = [
    re.compile(r'^more\s+(?:.*\s+)?(?:news|articles|stories|headlines)$', re.IGNORECASE),
    re.compile(r'^related\s+(?:.*\s+)?(?:stories|articles|posts|news|content)$', re.IGNORECASE),
    re.compile(r'^you\s+may\s+also\s+like$', re.IGNORECASE),
    re.compile(r'^recommended\s+for\s+you$', re.IGNORECASE),
    re.compile(r'^read\s+next$', re.IGNORECASE),
    re.compile(r'^sponsored\s+(?:.*\s+)?content$', re.IGNORECASE),
    re.compile(r'^latest\s+(?:.*\s+)?(?:stories|news|headlines|articles)$', re.IGNORECASE),
    re.compile(r'^popular\s+(?:.*\s+)?(?:stories|articles|posts)$', re.IGNORECASE),
    re.compile(r'^top\s+stories$', re.IGNORECASE),
    re.compile(r'^trending\s+(?:.*\s+)?(?:stories|news|topics)$', re.IGNORECASE),
    re.compile(r'^comments$', re.IGNORECASE),
    re.compile(r'^discussion$', re.IGNORECASE),
    re.compile(r'^share\s+this\s+article$', re.IGNORECASE),
    re.compile(r'^follow\s+us$', re.IGNORECASE),
    re.compile(r'^newsletter$', re.IGNORECASE),
    re.compile(r'^more\s+from\s+', re.IGNORECASE),
]


def score_dom_element(element) -> float:
    """
    Heuristically scores a DOM element based on its likelihood of being the primary
    content container. High scores are awarded for paragraph density, semantic tags,
    and class names, while penalties are applied for navigation/sidebar terms and link density.
    """
    if not element.name or element.name in ("script", "style", "nav", "footer", "header", "aside", "noscript", "form"):
        return -1000.0

    text = element.get_text(separator=" ")
    text_len = len(text.strip())
    if text_len < 30:
        return -100.0

    # Calculate Link Density
    link_text = ""
    for a in element.find_all("a"):
        link_text += a.get_text(separator=" ")
    link_len = len(link_text.strip())
    
    link_density = link_len / text_len if text_len > 0 else 0.0
    if link_density > 0.5:
        # High link density indicates navigation blocks, menus, or footer links
        return -500.0

    score = 0.0

    # Paragraph count bonus
    p_count = len(element.find_all("p", recursive=True))
    score += p_count * 50.0

    # Text length bonus
    score += text_len * 0.1

    # Semantic element bonuses
    if element.name in ("article", "main"):
        score += 600.0
    elif element.name == "section":
        score += 150.0

    # Class and ID matching bonuses / penalties
    element_id = (element.get("id") or "").lower()
    element_classes = " ".join(element.get("class") or []).lower()
    combined_attrs = f"{element_id} {element_classes}"

    positive_keywords = ["content", "article", "post", "body", "entry", "main", "story", "text"]
    negative_keywords = ["menu", "nav", "sidebar", "footer", "header", "comment", "widget", "ad-", "ads", "share", "social", "banner", "cookie"]

    for kw in positive_keywords:
        if kw in combined_attrs:
            score += 200.0
    for kw in negative_keywords:
        if kw in combined_attrs:
            score -= 400.0

    return score

def extract_primary_content_container(soup: BeautifulSoup):
    """
    Finds the DOM element with the highest content density score.
    Applies hierarchical pruning to reject wrapper elements that contain high-density sub-elements,
    ensuring we isolate the actual article text container and exclude sidebars/menus.
    """
    candidates = []
    # Search candidates: div, section, article, main
    for element in soup.find_all(["div", "section", "article", "main"]):
        score = score_dom_element(element)
        if score > 100.0:
            candidates.append((element, score))

    if not candidates:
        return soup.find("body") or soup

    # Sort candidates by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)

    # Pruning: If a candidate A contains a descendant candidate B where B has at least 85% of A's paragraph tags,
    # then A is likely just a wrapper container. We should prefer B.
    for i in range(len(candidates)):
        el_a, score_a = candidates[i]
        if el_a is None:
            continue
        p_a = len(el_a.find_all("p"))
        if p_a == 0:
            continue
            
        # Find if there's any descendant candidate B
        for j in range(len(candidates)):
            if i == j:
                continue
            el_b, score_b = candidates[j]
            if el_b is None:
                continue
                
            # Check if el_b is a descendant of el_a
            if any(el_b is desc for desc in el_a.descendants):
                p_b = len(el_b.find_all("p"))
                if p_b >= 0.85 * p_a:
                    candidates[i] = (None, 0.0)
                    break

    # Filter out pruned candidates
    valid_candidates = [c for c in candidates if c[0] is not None]
    if valid_candidates:
        valid_candidates.sort(key=lambda x: x[1], reverse=True)
        return valid_candidates[0][0]
        
    return soup.find("body") or soup

def apply_stop_patterns(t_soup):
    stop_tag = None
    for tag in t_soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section"]):
        txt = tag.get_text().strip()
        if txt:
            tag_name = tag.name.lower()
            is_heading = tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]
            if is_heading or len(txt) < 80:
                is_stop = False
                for pattern in STOP_PATTERNS:
                    if pattern.match(txt):
                        is_stop = True
                        break
                if is_stop:
                    stop_tag = tag
                    break
    if stop_tag:
        to_decompose = list(stop_tag.next_elements)
        stop_tag.decompose()
        for el in to_decompose:
            try:
                el.decompose()
            except Exception:
                pass

def clean_element(element):
    """
    Recursively removes boilerplate, cookie banners, advertisements,
    and low-value elements from the target DOM node.
    """
    # Clone element to avoid modifying the original soup
    cleaned_copy = BeautifulSoup(str(element), "html.parser")
    
    # 1. Remove comments
    for comment in cleaned_copy.find_all(text=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 2. Decompose technical/low-value tags
    boilerplate_tags = ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe", "svg", "form", "button", "input", "select", "option"]
    for tag in cleaned_copy.find_all(boilerplate_tags):
        tag.decompose()

    # 3. Decompose elements by classes or IDs containing advertisement/cookie/navigation keywords
    ad_cookie_pattern = re.compile(
        r"cookie|banner|popup|modal|ad-wrapper|advertisement|sidebar|widget|share-|social-|"
        r"nav-|menu|toc|navigation|prev-next|left-menu|right-menu|header|footer|aside|breadcrumb|toolbar|comment|meta", 
        re.I
    )
    for tag in cleaned_copy.find_all(True):
        if not hasattr(tag, "attrs") or tag.attrs is None:
            continue
        tag_id = tag.get("id") or ""
        tag_classes = " ".join(tag.get("class") or [])
        if ad_cookie_pattern.search(tag_id) or ad_cookie_pattern.search(tag_classes):
            tag.decompose()

    apply_stop_patterns(cleaned_copy)
    return cleaned_copy

def safe_clean_element(element):
    """
    A minimal clean that only removes technical/non-content tags
    without class/ID name heuristics, ensuring maximum possible content retention.
    """
    cleaned_copy = BeautifulSoup(str(element), "html.parser")
    
    # Remove comments
    for comment in cleaned_copy.find_all(text=lambda text: isinstance(text, Comment)):
        comment.extract()
        
    # Remove non-content technical tags only
    technical_tags = ["script", "style", "noscript", "svg", "form", "button", "input", "select", "option"]
    for tag in cleaned_copy.find_all(technical_tags):
        tag.decompose()
        
    apply_stop_patterns(cleaned_copy)
    return cleaned_copy

def html_to_markdown(element, base_url: str = "") -> str:
    """
    Converts a BeautifulSoup DOM tree into clean, standard Markdown.
    Links are rendered as plain anchor text and media is omitted to maximize text fidelity.
    """
    if isinstance(element, NavigableString):
        text = str(element)
        return text

    if not element.name:
        return ""

    # Recurse helper
    def get_children_markdown(el):
        parts = []
        for child in el.children:
            parts.append(html_to_markdown(child, base_url))
        return "".join(parts)

    name = element.name.lower()

    # Boilerplate / Tech tags and media elements are ignored in text body to keep content lossless and clean
    if name in ("script", "style", "nav", "footer", "header", "aside", "noscript", 
                "form", "button", "select", "video", "audio", "iframe", "embed", "object", "img"):
        return ""

    elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        text_content = get_children_markdown(element).strip()
        # Strip permalink characters like ¶ or § (often wrapped in brackets)
        text_content = re.sub(r'\s*\[?¶\]?\s*$', '', text_content)
        text_content = re.sub(r'\s*\[?§\]?\s*$', '', text_content)
        text_content = text_content.replace("\n", " ")
        if not text_content:
            return ""
        return f"\n\n{'#' * level} {text_content}\n\n"

    elif name == "p":
        text_content = get_children_markdown(element).strip()
        if not text_content:
            return ""
        return f"\n\n{text_content}\n\n"

    elif name == "br":
        return "\n"

    elif name == "a":
        # Extract text content only to satisfy Firecrawl plain text markdown body requirement
        text_content = get_children_markdown(element)
        text_content = text_content.replace("\n", " ")
        return text_content

    elif name in ("ul", "ol"):
        items = []
        for li in element.find_all("li", recursive=False):
            item_md = html_to_markdown(li, base_url).strip()
            if item_md:
                bullet = "-" if name == "ul" else "1."
                items.append(f"{bullet} {item_md}")
        if not items:
            return ""
        return "\n\n" + "\n".join(items) + "\n\n"

    elif name == "li":
        return get_children_markdown(element)

    elif name == "table":
        rows = []
        for tr in element.find_all("tr"):
            cells = tr.find_all(["th", "td"], recursive=False)
            cell_texts = []
            for cell in cells:
                c_md = html_to_markdown(cell, base_url).strip()
                c_md = c_md.replace("\n", " ").replace("|", "\\|")
                cell_texts.append(c_md)
            if cell_texts:
                rows.append("| " + " | ".join(cell_texts) + " |")
                # Add border separator after header or first row
                if any(c.name == "th" for c in cells) or len(rows) == 1:
                    rows.append("| " + " | ".join("---" for _ in cell_texts) + " |")
        if not rows:
            return ""
        return "\n\n" + "\n".join(rows) + "\n\n"

    elif name in ("th", "td"):
        return get_children_markdown(element)

    elif name in ("pre", "code"):
        if name == "code" and element.parent and element.parent.name == "pre":
            return get_children_markdown(element)
        code_text = element.get_text()
        return f"\n\n```\n{code_text}\n```\n\n"

    elif name in ("blockquote", "q"):
        content = get_children_markdown(element).strip()
        if content:
            return f"\n\n> {content}\n\n"
        return ""

    elif name in ("strong", "b"):
        content = get_children_markdown(element).strip()
        return f" **{content}** " if content else ""

    elif name in ("em", "i"):
        content = get_children_markdown(element).strip()
        return f" *{content}* " if content else ""

    else:
        return get_children_markdown(element)

def strip_raw_html_tags(text: str) -> str:
    """
    Removes leaked HTML formatting tags (like <font ...>, <span>, </font>, etc.)
    from the plain text, leaving clean human-readable content.
    """
    tag_pattern = re.compile(
        r'</?(?:font|span|u|b|i|em|strong|p|div|a|code|pre|br|li|ul|ol|table|tr|td|th|section|article|figure|figcaption|iframe|embed|object|video|audio)(?:\s+[^>]*?)?>',
        re.IGNORECASE
    )
    return tag_pattern.sub('', text)

def normalize_whitespace(text: str) -> str:
    """Normalizes excessive newlines and whitespace segments in Markdown."""
    text = strip_raw_html_tags(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

def parse_dimension(value) -> int:
    if not value:
        return 0
    match = re.search(r'\d+', str(value))
    return int(match.group()) if match else 0

def get_meaningful_text_length(element) -> int:
    """
    Computes total length of text content inside readable elements to evaluate extraction retention.
    """
    if not element:
        return 0
    text_parts = []
    for el in element.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "pre", "code", "blockquote", "q"]):
        text_parts.append(el.get_text().strip())
    combined = " ".join(text_parts)
    combined = re.sub(r'\s+', ' ', combined).strip()
    return len(combined)

def parse_structured_content(cleaned_element, base_url: str = "") -> Dict[str, List]:
    """
    Extracts structured page segments matching Firecrawl content requirements:
    headings, paragraphs, lists, tables, code blocks, and blockquotes.
    """
    headings = []
    paragraphs = []
    lists = []
    tables = []
    code_blocks = []
    quotes = []

    # 1. Extract Headings
    for h in cleaned_element.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        h_text = h.get_text().strip()
        h_text = re.sub(r'\s*\[?¶\]?\s*$', '', h_text)
        h_text = re.sub(r'\s*\[?§\]?\s*$', '', h_text)
        h_text = re.sub(r'\s+', ' ', h_text)
        h_text = strip_raw_html_tags(h_text)
        if h_text and h_text not in headings:
            headings.append(h_text)

    # 2. Extract Paragraphs
    for p in cleaned_element.find_all("p"):
        p_text = p.get_text().strip()
        p_text = re.sub(r'\s+', ' ', p_text)
        p_text = strip_raw_html_tags(p_text)
        if p_text and p_text not in paragraphs:
            paragraphs.append(p_text)

    # 3. Extract Lists
    for lst in cleaned_element.find_all(["ul", "ol"]):
        items = []
        for li in lst.find_all("li", recursive=False):
            li_text = li.get_text().strip()
            li_text = re.sub(r'\s+', ' ', li_text)
            li_text = strip_raw_html_tags(li_text)
            if li_text:
                items.append(li_text)
        if items and items not in lists:
            lists.append(items)

    # 4. Extract Tables
    for tbl in cleaned_element.find_all("table"):
        tbl_data = []
        for tr in tbl.find_all("tr"):
            row = []
            for cell in tr.find_all(["th", "td"], recursive=False):
                cell_text = cell.get_text().strip()
                cell_text = re.sub(r'\s+', ' ', cell_text)
                cell_text = strip_raw_html_tags(cell_text)
                row.append(cell_text)
            if row:
                tbl_data.append(row)
        if tbl_data and tbl_data not in tables:
            tables.append(tbl_data)

    # 5. Extract Code Blocks
    for cb in cleaned_element.find_all(["pre", "code"]):
        if cb.name == "code" and cb.parent and cb.parent.name == "pre":
            continue
        cb_text = cb.get_text().strip()
        cb_text = strip_raw_html_tags(cb_text)
        if cb_text and cb_text not in code_blocks:
            code_blocks.append(cb_text)

    # 6. Extract Quotes
    for q in cleaned_element.find_all(["blockquote", "q"]):
        q_text = q.get_text().strip()
        q_text = re.sub(r'\s+', ' ', q_text)
        q_text = strip_raw_html_tags(q_text)
        if q_text and q_text not in quotes:
            quotes.append(q_text)

    return {
        "headings": headings,
        "paragraphs": paragraphs,
        "lists": lists,
        "tables": tables,
        "codeBlocks": code_blocks,
        "quotes": quotes
    }

def convert_html_to_firecrawl_schema(html: str, url: str, status_code: int = 200, soup=None) -> Dict[str, Any]:
    """
    Parses raw HTML, filters boilerplate content, converts it into Clean Markdown,
    extracts metadata, resolves and structures separate images, videos, and links.
    """
    soup = soup or BeautifulSoup(html, "html.parser")

    # BUGFIX: BeautifulSoup tag.get() can return None if the attribute is present but has no value. We change tag.get("attr", "") to (tag.get("attr") or "") before calling .strip() to prevent AttributeError.
    # 1. Metadata Extraction
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else ""
    if not title:
        og_title = soup.find("meta", attrs={"property": "og:title"})
        title = (og_title.get("content") or "").strip() if og_title else ""
    if not title:
        h1_tag = soup.find("h1")
        title = h1_tag.get_text().strip() if h1_tag else ""
    if not title:
        title = "No Title"
    title = strip_raw_html_tags(title)

    desc_meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = (desc_meta.get("content") or "").strip() if desc_meta else ""
    description = strip_raw_html_tags(description)

    html_tag = soup.find("html")
    language = (html_tag.get("lang") or "en").strip().split("-")[0].lower() if html_tag else "en"

    # 2. Extract and Deduplicate Links, Images, and Videos from full document (lossless metadata mapping)
    links_list: List[Dict[str, str]] = []
    seen_links: Set[Tuple[str, str, str]] = set()

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            absolute_href = urllib.parse.urljoin(url, href)
            text = a.get_text().strip()
            text = re.sub(r'\s+', ' ', text)
            text = strip_raw_html_tags(text)
            title_attr = (a.get("title") or "").strip()
            title_attr = strip_raw_html_tags(title_attr)
            
            key = (absolute_href, text, title_attr)
            if key not in seen_links:
                seen_links.add(key)
                links_list.append({
                    "text": text,
                    "url": absolute_href,
                    "title": title_attr
                })

    images_list: List[Dict[str, Any]] = []
    seen_images: Set[str] = set()

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if src:
            absolute_src = urllib.parse.urljoin(url, src)
            if absolute_src not in seen_images:
                seen_images.add(absolute_src)
                alt = (img.get("alt") or "").strip()
                alt = strip_raw_html_tags(alt)
                
                caption = ""
                parent = img.parent
                if parent and parent.name == "figure":
                    figcaption = parent.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text().strip()
                elif parent and parent.parent and parent.parent.name == "figure":
                    figcaption = parent.parent.find("figcaption")
                    if figcaption:
                        caption = figcaption.get_text().strip()
                            
                if not caption:
                    caption = (img.get("title") or "").strip() or (img.get("caption") or "").strip()
                caption = strip_raw_html_tags(caption)
                            
                width = parse_dimension(img.get("width"))
                height = parse_dimension(img.get("height"))
                
                images_list.append({
                    "src": absolute_src,
                    "alt": alt,
                    "caption": caption,
                    "width": width,
                    "height": height
                })

    videos_list: List[Dict[str, str]] = []
    seen_videos: Set[str] = set()

    # HTML5 video tags
    for video in soup.find_all("video"):
        src = (video.get("src") or "").strip()
        v_type = "html5"
        if not src:
            source_tag = video.find("source")
            if source_tag:
                src = (source_tag.get("src") or "").strip()
                v_type = (source_tag.get("type") or "").strip() or "html5"
        if src:
            abs_src = urllib.parse.urljoin(url, src)
            if abs_src not in seen_videos:
                seen_videos.add(abs_src)
                v_title = (video.get("title") or "").strip() or (video.get("name") or "").strip() or ""
                v_title = strip_raw_html_tags(v_title)
                thumbnail = (video.get("poster") or "").strip()
                if thumbnail:
                    thumbnail = urllib.parse.urljoin(url, thumbnail)
                videos_list.append({
                    "src": abs_src,
                    "title": v_title,
                    "thumbnail": thumbnail,
                    "type": v_type
                })

    # Video Embeds (YouTube, Vimeo, Loom, Wistia, Brightcove, iframe video providers)
    for iframe in soup.find_all(["iframe", "embed", "object"]):
        src = (iframe.get("src") or "").strip() or (iframe.get("data") or "").strip()
        if src:
            abs_src = urllib.parse.urljoin(url, src)
            v_title = (iframe.get("title") or "").strip() or ""
            v_title = strip_raw_html_tags(v_title)
            
            lower_src = abs_src.lower()
            is_video = False
            v_type = "iframe"
            
            if "youtube.com" in lower_src or "youtu.be" in lower_src:
                is_video = True
                v_type = "youtube"
            elif "vimeo.com" in lower_src:
                is_video = True
                v_type = "vimeo"
            elif "loom.com" in lower_src:
                is_video = True
                v_type = "loom"
            elif "wistia" in lower_src:
                is_video = True
                v_type = "wistia"
            elif "brightcove" in lower_src:
                is_video = True
                v_type = "brightcove"
            elif "video" in lower_src or "player" in lower_src or "embed" in lower_src:
                is_video = True
                v_type = "iframe"
                
            if is_video and abs_src not in seen_videos:
                seen_videos.add(abs_src)
                videos_list.append({
                    "src": abs_src,
                    "title": v_title,
                    "thumbnail": "",
                    "type": v_type
                })

    # 3. Primary Content Isolation & Validation Block
    primary_container = extract_primary_content_container(soup)
    original_text_len = get_meaningful_text_length(primary_container)

    # Apply aggressive clean
    cleaned_container = clean_element(primary_container)
    cleaned_text_len = get_meaningful_text_length(cleaned_container)

    # Perform self-healing text retention validator check
    retention_ratio = cleaned_text_len / original_text_len if original_text_len > 0 else 1.0
    
    print(f"[METRICS] Extraction Quality Check for: {url}")
    print(f"   - Original text content length: {original_text_len} chars")
    print(f"   - Cleaned text content length: {cleaned_text_len} chars")
    print(f"   - Computed content retention: {retention_ratio:.2%}")

    if retention_ratio < MIN_RETENTION_RATIO:
        print(f"   - [WARNING] Content retention falls below {MIN_RETENTION_RATIO:.2%} threshold. Rolling back to safe minimal cleaning.")
        cleaned_container = safe_clean_element(primary_container)
        fallback_len = get_meaningful_text_length(cleaned_container)
        fallback_ratio = fallback_len / original_text_len if original_text_len > 0 else 1.0
        print(f"   - Post-rollback content retention: {fallback_ratio:.2%}")

    # Try Trafilatura for Markdown output
    trafilatura_markdown = None
    try:
        import trafilatura
        trafilatura_markdown = trafilatura.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
        )
    except Exception:
        pass

    # Use Trafilatura markdown if it meets minimum length, else fall back to existing html_to_markdown()
    if trafilatura_markdown and len(trafilatura_markdown.strip()) >= MIN_EXTRACTED_SIZE:
        clean_markdown = trafilatura_markdown.strip()
    else:
        raw_markdown = html_to_markdown(cleaned_container, base_url=url)
        clean_markdown = normalize_whitespace(raw_markdown)

    # 4. Parse Structured Content
    structured_content = parse_structured_content(cleaned_container, base_url=url)

    # Cleaned HTML output
    cleaned_html = str(cleaned_container)

    return {
        "success": True,
        "data": {
            "markdown": clean_markdown,
            "html": cleaned_html,
            "metadata": {
                "title": title,
                "description": description,
                "language": language,
                "sourceURL": url,
                "statusCode": status_code
            },
            "links": links_list,
            "images": images_list,
            "videos": videos_list,
            "content": structured_content
        }
    }
