"""
Adaptive depth crawler - automatically increases depth when yields are low.

Strategy:
1. Start with depth=1 (fast)
2. If < 5 forms found, retry with depth=2
3. If still < 5 forms, try depth=3 (max)
4. Use smart path prioritization to speed up deeper crawls
"""
import re
from typing import List, Tuple
from urllib.parse import urlparse


def should_increase_depth(found_count: int, current_depth: int, max_depth: int = 3) -> bool:
    """
    Determine if we should increase crawl depth based on results.

    Args:
        found_count: Number of forms found at current depth
        current_depth: Current depth level
        max_depth: Maximum depth to try (default: 3)

    Returns:
        True if should retry with higher depth
    """
    if current_depth >= max_depth:
        return False

    # Thresholds for increasing depth
    DEPTH_THRESHOLDS = {
        1: 5,   # If < 5 forms at depth 1, try depth 2
        2: 3,   # If < 3 forms at depth 2, try depth 3
    }

    threshold = DEPTH_THRESHOLDS.get(current_depth, 0)
    return found_count < threshold


def prioritize_url(seed: str, url: str, current_depth: int) -> int:
    """
    Calculate priority score for a URL (higher = better).

    Used to prioritize which pages to crawl first, making deeper
    crawls faster by focusing on likely form pages.

    Args:
        seed: Original seed URL
        url: URL to prioritize
        current_depth: Current crawl depth

    Returns:
        Priority score (0-100)
    """
    score = 50  # Base score

    url_lower = url.lower()
    path = urlparse(url).path.lower()

    # High priority: form-related paths
    form_keywords = [
        r'/forms?/',
        r'/documents?/',
        r'/applications?/',
        r'/resources?/',
        r'/downloads?/',
        r'/pdfs?/',
        r'/templates?/',
        r'form',
        r'doc',
        r'app',
    ]

    for pattern in form_keywords:
        if re.search(pattern, path):
            score += 20
            break

    # Medium priority: same directory as seed
    seed_dir = '/'.join(urlparse(seed).path.split('/')[:-1])
    url_dir = '/'.join(urlparse(url).path.split('/')[:-1])

    if seed_dir and url_dir.startswith(seed_dir):
        score += 15

    # Penalty: deep paths (at higher depths, prefer shorter paths)
    path_depth = path.count('/')
    if current_depth >= 2:
        score -= min(path_depth * 2, 20)

    # Penalty: query strings (often not useful for forms)
    if '?' in url:
        score -= 10

    # Penalty: common non-form paths
    avoid_patterns = [
        r'/blog',
        r'/news',
        r'/press',
        r'/about',
        r'/contact',
        r'/career',
        r'/help',
        r'/support',
        r'/search',
        r'/login',
        r'/signup',
        r'/account',
    ]

    for pattern in avoid_patterns:
        if re.search(pattern, path):
            score -= 15
            break

    # Clamp to 0-100
    return max(0, min(100, score))


def get_adaptive_deadline(depth: int, base_deadline: float = 240.0) -> float:
    """
    Calculate deadline based on depth (deeper = more time allowed).

    Args:
        depth: Crawl depth
        base_deadline: Base deadline in seconds

    Returns:
        Adjusted deadline in seconds
    """
    # Scale deadline by depth
    multipliers = {
        1: 1.0,   # 240s (4 min)
        2: 1.5,   # 360s (6 min)
        3: 2.0,   # 480s (8 min)
    }

    multiplier = multipliers.get(depth, 1.0)
    return base_deadline * multiplier


def get_adaptive_page_limit(depth: int, base_limit: int = 750) -> int:
    """
    Calculate page limit based on depth (deeper = more pages).

    Args:
        depth: Crawl depth
        base_limit: Base page limit

    Returns:
        Adjusted page limit
    """
    # Allow more pages at higher depths
    multipliers = {
        1: 0.5,   # 375 pages
        2: 1.0,   # 750 pages
        3: 1.5,   # 1125 pages
    }

    multiplier = multipliers.get(depth, 1.0)
    return int(base_limit * multiplier)


def should_follow_path(seed: str, url: str, depth: int) -> bool:
    """
    Determine if a URL path should be followed at given depth.

    At higher depths, be more selective about which paths to follow.

    Args:
        seed: Original seed URL
        url: URL to evaluate
        depth: Current crawl depth

    Returns:
        True if should follow this path
    """
    if depth <= 1:
        # At depth 1, follow most paths (existing logic)
        return True

    # At depth 2+, be more selective
    priority = prioritize_url(seed, url, depth)

    # Require higher priority at deeper levels
    thresholds = {
        2: 40,  # At depth 2, require priority >= 40
        3: 60,  # At depth 3, require priority >= 60
    }

    threshold = thresholds.get(depth, 50)
    return priority >= threshold


def format_adaptive_reason(attempts: List[Tuple[int, int]], final_reason: str) -> str:
    """
    Format reason string for adaptive crawl results.

    Args:
        attempts: List of (depth, found_count) tuples
        final_reason: Final crawl termination reason

    Returns:
        Formatted reason string
    """
    if len(attempts) == 1:
        return final_reason

    # Multiple attempts - show progression
    depth_summary = " → ".join([f"d{d}:{c}" for d, c in attempts])
    return f"adaptive ({depth_summary}) - {final_reason}"
