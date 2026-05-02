# app/services/pdf_vision_analyzer.py
"""
PDF Vision Analysis using Claude's vision API for scanned/flat PDFs.
Handles actionability detection and detailed form element extraction.
"""
from __future__ import annotations

import os
import io
import base64
import logging
from typing import Dict, Any, List, Optional
from PIL import Image

try:
    from pdf2image import convert_from_bytes
except ImportError:
    convert_from_bytes = None

try:
    import pypdf
except ImportError:
    pypdf = None

from app.services.llm_router import chat_complete, LLMError

logger = logging.getLogger(__name__)

# Vision analysis prompt with actionability detection
VISION_PROMPT = '''Analyze this document and FIRST determine if it's actionable.

ACTIONABILITY ASSESSMENT (CRITICAL - EVALUATE FIRST):
Is this document actionable? A document is actionable if it:
1. Collects information (has fields for user input)
2. Requires signatures (has signature lines/fields)
3. Serves as a regulatory disclosure/notice requiring acknowledgment
4. Is a form, application, request, or claim document

NOT actionable (exclude these):
- Marketing brochures or promotional materials
- Informational handbooks or guides
- Reports or research papers
- Presentations or slide decks
- General instructions without a form
- News articles or announcements
- Training materials

If NOT actionable, return: {"is_actionable": false, "document_type": "[type of non-actionable document]"}

If actionable, analyze and extract:

SIGNATURES:
- Count all signature fields/lines
- Note any conditional signature logic ("if amount > X, additional signature required")
- Witness signature requirements

ATTACHMENTS (CRITICAL):
- List ALL required attachments mentioned ("attach W-2", "include bank statement", etc.)
- Note document checklists

ID VERIFICATION (CRITICAL):
- Any photo ID/driver's license/passport requirements NOT related to notarization
- Copy of ID requirements

CONDITIONAL LOGIC (CRITICAL):
- All if/then patterns ("If yes, complete Section B")
- Skip instructions ("Skip to line 7 if...")
- Fields that become required based on other fields
- Variable requirements based on amounts/dates

FIELDS:
- Count of fillable fields (text boxes, lines)
- Checkboxes
- Radio button groups

Return JSON:
{
  "is_actionable": true/false,
  "document_type": "form|disclosure|handbook|brochure|report|other",
  "actionability_reason": "",
  "signature_count": 0,
  "conditional_signer_logic": "",
  "attachments_required": [],
  "id_verification_required": false,
  "id_verification_type": "",
  "conditional_logic_present": false,
  "conditional_logic_description": [],
  "field_count": 0,
  "checkbox_count": 0,
  "collects_information": true/false,
  "requires_signature": true/false,
  "is_regulatory_disclosure": true/false
}'''


def _pdf_to_images(pdf_bytes: bytes, max_pages: int = 3, dpi: int = 150, max_dimension: int = 2048) -> List[bytes]:
    """
    Convert PDF pages to PNG images using smart sampling.

    Args:
        pdf_bytes: Raw PDF bytes
        max_pages: Maximum pages to convert (default 3)
        dpi: DPI for conversion (default 150)
        max_dimension: Maximum width/height in pixels (default 2048)

    Returns:
        List of PNG image bytes
    """
    if convert_from_bytes is None:
        raise ImportError("pdf2image not installed. Run: pip install pdf2image")

    try:
        # First, get total page count
        if pypdf is None:
            raise ImportError("pypdf not installed. Run: pip install pypdf")

        pdf_reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(pdf_reader.pages)

        # Smart sampling: first, middle, last pages (up to max_pages)
        if total_pages <= max_pages:
            # Convert all pages if PDF is small
            page_numbers = list(range(1, total_pages + 1))
        else:
            # Sample strategically: first, middle, last
            page_numbers = [
                1,  # First page (cover/instructions)
                total_pages // 2,  # Middle page (likely form content)
                total_pages  # Last page (often signatures)
            ][:max_pages]

        logger.info(f"Sampling {len(page_numbers)} pages from {total_pages}-page PDF: {page_numbers}")

        # Convert selected pages to PIL images
        images = []
        for page_num in page_numbers:
            page_images = convert_from_bytes(pdf_bytes, dpi=dpi, first_page=page_num, last_page=page_num)
            images.extend(page_images)

        png_images = []
        for img in images:
            # Resize if too large
            width, height = img.size
            if width > max_dimension or height > max_dimension:
                ratio = min(max_dimension / width, max_dimension / height)
                new_size = (int(width * ratio), int(height * ratio))
                img = img.resize(new_size, Image.LANCZOS)

            # Convert to PNG bytes
            buffer = io.BytesIO()
            img.save(buffer, format='PNG', optimize=True)
            png_images.append(buffer.getvalue())

        logger.info(f"Converted {len(png_images)} PDF pages to images")
        return png_images

    except Exception as e:
        logger.error(f"Failed to convert PDF to images: {e}")
        raise


def _encode_image(image_bytes: bytes) -> str:
    """Encode image bytes to base64 string."""
    return base64.standard_b64encode(image_bytes).decode('utf-8')


def _build_vision_messages(image_data_list: List[str], provider: str = "anthropic") -> List[Dict[str, Any]]:
    """
    Build provider-specific vision messages with multiple images.

    Args:
        image_data_list: List of base64-encoded image strings
        provider: LLM provider (anthropic or openai)

    Returns:
        Messages list in provider-specific format
    """
    # Build content with all images + prompt
    content = []

    # Add all images first
    for img_data in image_data_list:
        if provider == "openai":
            # OpenAI format: image_url with data URI
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{img_data}"
                }
            })
        else:
            # Anthropic format: image with base64 source
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_data
                }
            })

    # Add text prompt
    content.append({
        "type": "text",
        "text": VISION_PROMPT
    })

    return [
        {"role": "system", "content": "You are a form analysis expert. Extract structured data from documents."},
        {"role": "user", "content": content}
    ]


def _parse_vision_response(response_text: str) -> Dict[str, Any]:
    """
    Parse Claude's vision response into structured data.

    Args:
        response_text: Raw text response from Claude

    Returns:
        Parsed dictionary
    """
    import json
    import re

    # Try to extract JSON from response
    # Claude might return markdown code blocks
    json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', response_text)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            json_str = json_match.group(0)
        else:
            logger.warning("No JSON found in vision response, using defaults")
            return {
                "is_actionable": True,  # Default to actionable on parse failure
                "document_type": "unknown",
                "error": "Failed to parse vision response"
            }

    try:
        data = json.loads(json_str)
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse vision JSON: {e}")
        return {
            "is_actionable": True,  # Default to actionable on parse failure
            "document_type": "unknown",
            "error": f"JSON parse error: {e}"
        }


def analyze_flat_pdf_with_vision(
    pdf_bytes: bytes,
    source_url: str = "",
    provider: Optional[str] = None,
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Analyze a flat/scanned PDF using Claude's vision API.

    Args:
        pdf_bytes: Raw PDF bytes
        source_url: Source URL for logging
        provider: LLM provider (defaults to env or 'anthropic')
        model: LLM model (defaults to claude-3-5-sonnet-20241022 for vision)

    Returns:
        Dictionary with vision analysis results
    """
    # Check if vision analysis is enabled
    if os.getenv("ENABLE_VISION_ANALYSIS", "true").lower() not in ("true", "1", "yes"):
        logger.info("Vision analysis disabled via ENABLE_VISION_ANALYSIS")
        return {"is_actionable": True, "vision_analyzed": False, "reason": "disabled"}

    # Get config
    max_pages = int(os.getenv("VISION_MAX_PAGES", "3"))
    provider = provider or os.getenv("LLM_PROVIDER", "anthropic")

    # Select vision-capable model based on provider
    if not model:
        if provider == "openai":
            model = os.getenv("OPENAI_MODEL", "gpt-4o")  # Vision-capable OpenAI model
        elif provider == "anthropic":
            model = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")  # Vision-capable Claude model
        else:
            model = "claude-3-5-sonnet-20241022"  # Default fallback

    try:
        logger.info(f"Starting vision analysis for PDF from {source_url}")

        # Convert PDF to images
        images = _pdf_to_images(pdf_bytes, max_pages=max_pages)

        if not images:
            logger.warning("No images extracted from PDF")
            return {"is_actionable": True, "vision_analyzed": False, "reason": "no_images"}

        # Encode images to base64
        encoded_images = [_encode_image(img) for img in images]

        # Build vision messages (provider-specific format)
        messages = _build_vision_messages(encoded_images, provider=provider)

        # Call vision API
        logger.info(f"Calling {provider} vision API with {len(encoded_images)} images")
        response = chat_complete(
            provider=provider,
            model=model,
            messages=messages,
            max_tokens=2000,
            temperature=0.0,
            timeout=60.0,
            retries=1,
            fallback=False
        )

        # Log raw response for debugging
        logger.info(f"[VISION DEBUG] Raw Claude response:\n{response}")

        # Parse response
        vision_data = _parse_vision_response(response)
        vision_data["vision_analyzed"] = True
        vision_data["vision_pages_analyzed"] = len(images)

        # Log actionability determination
        is_actionable = vision_data.get("is_actionable", True)
        doc_type = vision_data.get("document_type", "unknown")
        reason = vision_data.get("actionability_reason", "")

        if is_actionable:
            logger.info(f"✓ Document marked as ACTIONABLE ({doc_type}): {source_url}")
        else:
            logger.warning(f"✗ Document marked as NON-ACTIONABLE ({doc_type}): {source_url} - {reason}")

        return vision_data

    except LLMError as e:
        logger.error(f"LLM error during vision analysis: {e}")
        return {
            "is_actionable": True,  # Default to actionable on error
            "vision_analyzed": False,
            "error": str(e),
            "reason": "llm_error"
        }

    except Exception as e:
        logger.error(f"Vision analysis failed: {e}", exc_info=True)
        return {
            "is_actionable": True,  # Default to actionable on error
            "vision_analyzed": False,
            "error": str(e),
            "reason": "exception"
        }


def merge_vision_results_into_record(record: Dict[str, Any], vision_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge vision analysis results into the main analysis record.

    Args:
        record: Main PDF analysis record
        vision_data: Vision analysis results

    Returns:
        Updated record
    """
    # Store raw vision data
    record["_vision_data"] = vision_data
    record["vision_analyzed"] = vision_data.get("vision_analyzed", False)

    # Actionability
    record["is_actionable"] = vision_data.get("is_actionable", True)
    record["document_type"] = vision_data.get("document_type", "unknown")

    if not vision_data.get("is_actionable", True):
        # Non-actionable document - minimal processing
        record["actionability_reason"] = vision_data.get("actionability_reason", "")
        return record

    # Update signature analysis
    sig_count = vision_data.get("signature_count", 0)
    if sig_count > 0:
        if "signature_analysis" not in record:
            record["signature_analysis"] = {}
        record["signature_analysis"]["signature_count"] = max(
            record["signature_analysis"].get("signature_count", 0),
            sig_count
        )
        record["signature_analysis"]["vision_detected"] = True

        if vision_data.get("conditional_signer_logic"):
            record["signature_analysis"]["conditional_logic"] = vision_data["conditional_signer_logic"]

    # Update attachments
    attachments = vision_data.get("attachments_required", [])
    if attachments:
        record["attachments_required"] = True
        record["attachment_list"] = attachments
        record["attachment_count"] = len(attachments)

    # Update ID verification
    if vision_data.get("id_verification_required"):
        record["identification_required"] = True
        record["id_verification_type"] = vision_data.get("id_verification_type", "")

    # Update conditional logic
    if vision_data.get("conditional_logic_present"):
        record["conditional_logic"] = True
        record["conditional_logic_details"] = vision_data.get("conditional_logic_description", [])

    # Update field counts in signature_analysis dict (analyzer expects them there)
    if "signature_analysis" not in record:
        record["signature_analysis"] = {}

    widgets = record["signature_analysis"]

    # Field count
    vision_field_count = vision_data.get("field_count", 0)
    widgets["field_count"] = max(
        widgets.get("field_count", 0),
        vision_field_count
    )

    # Checkbox count
    checkbox_count = vision_data.get("checkbox_count", 0)
    widgets["checkboxes"] = max(
        widgets.get("checkboxes", 0),
        checkbox_count
    )

    # Also update top-level field_count for backward compatibility
    record["field_count"] = widgets["field_count"]

    # Add vision-specific flags
    record["collects_information"] = vision_data.get("collects_information", False)
    record["requires_signature"] = vision_data.get("requires_signature", False)
    record["is_regulatory_disclosure"] = vision_data.get("is_regulatory_disclosure", False)

    return record
