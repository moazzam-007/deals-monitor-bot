import re
import hashlib
import logging
import threading
import requests
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

_local = threading.local()

def _get_session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        _local.session.max_redirects = 10
    return _local.session

# Domains that need HTTP redirect resolution
SHORTENED_DOMAINS = [
    "amzn.to", "a.co",
    "fkrt.it", "fkrt.cc",
    "myntr.it",
    "bittli.in", "bitli.in",
    "bit.ly", "tinyurl.com",
    "ekaro.in", "earnkaro.com",
]

# E-commerce domains we care about
ECOMMERCE_DOMAINS = [
    "amazon.in", "amazon.com", "amazon.co.uk",
    "flipkart.com",
    "myntra.com",
    "ajio.com",
    "nykaa.com", "nykaafashion.com",
    "meesho.com",
    "snapdeal.com",
    "jiomart.com",
    "tatacliq.com",
    "shopsy.in",
]

# Combined list for URL detection
ALL_KNOWN_DOMAINS = SHORTENED_DOMAINS + ECOMMERCE_DOMAINS


def _domain_matches(netloc, domain):
    """Exact domain match: 'www.amazon.in' matches 'amazon.in', but 'amazon.in.evil.com' does not."""
    netloc = netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc == domain or netloc.endswith("." + domain)


def _any_domain_matches(netloc, domain_list):
    """Check if netloc matches any domain in the list."""
    return any(_domain_matches(netloc, d) for d in domain_list)


class URLResolver:
    # ------------------------------------------------------------------
    # URL extraction from message text
    # ------------------------------------------------------------------
    def extract_urls(self, text):
        """Extract all HTTP/HTTPS URLs from text."""
        if not text:
            return []
        urls = re.findall(r"https?://[^\s<>\"')\]]+", text, re.IGNORECASE)
        # Clean trailing punctuation that may have been captured
        cleaned = []
        for url in urls:
            url = url.rstrip(".,;:!?")
            if url not in cleaned:
                cleaned.append(url)
        return cleaned

    def is_product_url(self, url):
        """Check if URL belongs to a known e-commerce or shortener domain."""
        try:
            netloc = urlparse(url).netloc.lower()
            return _any_domain_matches(netloc, ALL_KNOWN_DOMAINS)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Shortened URL resolution
    # ------------------------------------------------------------------
    def resolve_url(self, url):
        """Resolve shortened URLs by following redirects. Returns final URL."""
        try:
            netloc = urlparse(url).netloc.lower()
            if not _any_domain_matches(netloc, SHORTENED_DOMAINS):
                return url
            
            session = _get_session()
            response = session.head(
                url, allow_redirects=True, timeout=10
            )
            final = response.url
            logger.info(f"Resolved {url} -> {final}")
            return final
        except requests.exceptions.RequestException:
            # Fallback: try GET request
            try:
                session = _get_session()
                response = session.get(
                    url, allow_redirects=True, timeout=10
                )
                return response.url
            except Exception as e:
                logger.warning(f"Failed to resolve {url}: {e}")
                return url

    # ------------------------------------------------------------------
    # Product ID extraction per platform
    # ------------------------------------------------------------------
    def _extract_amazon_id(self, parsed):
        """Extract ASIN from Amazon URL."""
        match = re.search(
            r"(?:/dp/|/gp/product/)([A-Z0-9]{10})", parsed.path
        )
        if match:
            return f"amz_{match.group(1)}"
        # Check query params
        params = parse_qs(parsed.query)
        for key in ("ASIN", "asin"):
            if key in params:
                return f"amz_{params[key][0]}"
        return None

    def _extract_flipkart_id(self, parsed):
        """Extract product ID from Flipkart URL."""
        # /product-name/p/ITEM_ID
        match = re.search(r"/p/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return f"fk_{match.group(1)}"
        # Query param: pid=XXXXX
        params = parse_qs(parsed.query)
        pid = params.get("pid", [None])[0]
        if pid:
            return f"fk_{pid}"
        return None

    def _extract_myntra_id(self, parsed):
        """Extract product ID from Myntra URL."""
        match = re.search(r"/(\d{5,})", parsed.path)
        if match:
            return f"myn_{match.group(1)}"
        return None

    def _extract_ajio_id(self, parsed):
        """Extract product ID from AJIO URL."""
        match = re.search(r"/p/([a-zA-Z0-9_]+)", parsed.path)
        if match:
            return f"ajio_{match.group(1)}"
        return None

    def _extract_meesho_id(self, parsed):
        """Extract product ID from Meesho URL."""
        match = re.search(r"/([a-zA-Z0-9-]+)/p/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return f"msh_{match.group(2)}"
        return None

    def extract_product_id(self, url):
        """Extract a platform-specific product ID from URL."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            if _any_domain_matches(domain, ["amazon.in", "amazon.com", "amazon.co.uk"]):
                pid = self._extract_amazon_id(parsed)
                if pid:
                    return pid

            elif _domain_matches(domain, "flipkart.com"):
                pid = self._extract_flipkart_id(parsed)
                if pid:
                    return pid

            elif _domain_matches(domain, "myntra.com"):
                pid = self._extract_myntra_id(parsed)
                if pid:
                    return pid

            elif _domain_matches(domain, "ajio.com"):
                pid = self._extract_ajio_id(parsed)
                if pid:
                    return pid

            elif _domain_matches(domain, "meesho.com"):
                pid = self._extract_meesho_id(parsed)
                if pid:
                    return pid

            # Fallback: MD5 hash of the cleaned URL
            clean = url.split("?")[0].rstrip("/")
            url_hash = hashlib.md5(clean.encode()).hexdigest()[:12]
            return f"url_{url_hash}"

        except Exception as e:
            logger.warning(f"Product ID extraction failed for {url}: {e}")
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            return f"url_{url_hash}"

    def detect_platform(self, url):
        """Detect which e-commerce platform the URL belongs to."""
        try:
            domain = urlparse(url).netloc.lower()
        except Exception:
            return "unknown"

        platform_map = {
            "amazon": ["amazon.in", "amazon.com", "amazon.co.uk"],
            "flipkart": ["flipkart.com"],
            "myntra": ["myntra.com"],
            "ajio": ["ajio.com"],
            "meesho": ["meesho.com"],
            "nykaa": ["nykaa.com", "nykaafashion.com"],
            "snapdeal": ["snapdeal.com"],
            "jiomart": ["jiomart.com"],
            "tatacliq": ["tatacliq.com"],
            "shopsy": ["shopsy.in"],
        }
        for platform, domains in platform_map.items():
            if _any_domain_matches(domain, domains):
                return platform
        return "unknown"

    # ------------------------------------------------------------------
    # Full processing pipeline
    # ------------------------------------------------------------------
    def process_url(self, url):
        """Complete pipeline: resolve shortened URL, extract product ID, detect platform."""
        resolved = self.resolve_url(url)
        product_id = self.extract_product_id(resolved)
        platform = self.detect_platform(resolved)

        return {
            "original_url": url,
            "resolved_url": resolved,
            "product_id": product_id,
            "platform": platform,
        }
