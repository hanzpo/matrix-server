"""Company tiering for notification grouping: "Shopify level and above"
(top-of-market comp: quant firms, FAANG-class, top AI labs and unicorns)
vs everything else. Matching is exact on the normalized name, so add
aliases rather than loosening the matcher.
"""
import re

_SUFFIX_RE = re.compile(
    r"\b(inc|llc|lp|llp|ltd|corp|corporation|co|group|holdings|technologies|labs)\.?$"
)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]")


def norm_company(name):
    s = _PUNCT_RE.sub("", name.lower().strip())
    s = re.sub(r"\s+", " ", s).strip()
    prev = None
    while s != prev:
        prev = s
        s = _SUFFIX_RE.sub("", s).strip()
    return s.removeprefix("the ").strip()


TIER1 = {norm_company(n) for n in [
    # quant / trading / hedge funds
    "Jane Street", "Citadel", "Citadel Securities", "Hudson River Trading",
    "HRT", "Two Sigma", "DRW", "Jump Trading", "Jump", "Five Rings",
    "Five Rings Capital", "Optiver", "IMC", "IMC Trading", "Akuna Capital",
    "Akuna", "SIG", "Susquehanna", "Susquehanna International Group",
    "DE Shaw", "D. E. Shaw", "Radix Trading", "Radix", "Tower Research Capital",
    "Old Mission", "Old Mission Capital", "Belvedere Trading", "CTC",
    "Chicago Trading Company", "Virtu Financial", "Virtu", "Flow Traders",
    "TransMarket Group", "DV Trading", "Geneva Trading", "Wolverine Trading",
    "Point72", "Cubist", "Millennium", "Millennium Management", "Balyasny",
    "Schonfeld", "Squarepoint", "Squarepoint Capital", "PDT Partners",
    "Voleon", "Voleon Capital", "AQR", "AQR Capital Management", "XTX Markets",
    "GTS", "Vatic Labs", "Aquatic Capital", "Aquatic Capital Management",
    "Marshall Wace", "Bridgewater", "Bridgewater Associates", "Jane Street Capital",
    "Valkyrie Trading", "Peak6", "3Red Partners", "Group One Trading",
    "Headlands Technologies", "Headlands Tech", "Quantlab", "Seven Eight Capital",
    "Walleye Capital", "Ansatz Capital", "PanAgora",
    # FAANG-class
    "Google", "Google DeepMind", "DeepMind", "Meta", "Apple", "Amazon",
    "Netflix", "Microsoft", "Nvidia", "AMD",
    # AI labs
    "OpenAI", "Anthropic", "xAI", "Perplexity", "Perplexity AI", "Cohere",
    "Mistral AI", "Cursor", "Anysphere", "Sierra", "Scale AI", "Scale",
    "Together AI", "Safe Superintelligence", "SSI", "Thinking Machines",
    "Thinking Machines Lab", "World Labs",
    # top-of-market tech
    "Stripe", "Databricks", "Snowflake", "Figma", "Notion", "Ramp", "Plaid",
    "Airbnb", "Uber", "Lyft", "DoorDash", "Coinbase", "Robinhood", "Roblox",
    "Pinterest", "Snap", "Snapchat", "TikTok", "ByteDance", "LinkedIn",
    "Datadog", "Cloudflare", "Palantir", "SpaceX", "Anduril",
    "Anduril Industries", "Waymo", "Tesla", "Brex", "Mercury", "Vercel",
    "Linear", "Vanta", "Verkada", "Neuralink", "Discord", "Reddit", "Airtable",
    "MongoDB", "Salesforce", "Adobe", "Atlassian", "Block", "Square", "Shopify",
    "Duolingo", "Affirm", "Instacart", "Samsara", "Zoox", "Rippling",
    "Retool", "Benchling", "Chime", "Gusto", "OpenSea", "Groq", "Cerebras",
    "Tenstorrent", "Etched", "Modal", "Modal Labs", "Temporal", "PlanetScale",
]}


def is_tier1(company):
    return norm_company(company) in TIER1
