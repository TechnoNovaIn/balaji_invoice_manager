import time
from typing import Dict, Any, Optional


class PerformanceTimer:
    """
    High-resolution timer for recording granular pipeline timings and operational metrics:
    - PDF opening
    - Page classification
    - Text extraction
    - Image rendering
    - Image enhancement
    - OCR processing
    - Invoice boundary segmentation
    - Gemini Vision API calls
    - Vendor matching
    - Database operations
    """

    def __init__(self, total_pages: int = 1):
        self.total_pages = max(1, total_pages)
        self._start_time = time.perf_counter()
        self._end_time: Optional[float] = None
        self._timers: Dict[str, float] = {
            'pdf_opening_ms': 0.0,
            'page_classification_ms': 0.0,
            'text_extraction_ms': 0.0,
            'image_rendering_ms': 0.0,
            'image_enhancement_ms': 0.0,
            'ocr_ms': 0.0,
            'segmentation_ms': 0.0,
            'gemini_api_ms': 0.0,
            'vendor_matching_ms': 0.0,
            'database_ops_ms': 0.0,
        }
        self._counters: Dict[str, int] = {
            'ocr_pages_count': 0,
            'digital_pages_count': 0,
            'mixed_pages_count': 0,
            'blank_pages_count': 0,
            'gemini_requests_count': 0,
            'invoices_detected_count': 0,
        }
        self._active_starts: Dict[str, float] = {}

    def start_timing(self, key: str):
        self._active_starts[key] = time.perf_counter()

    def stop_timing(self, key: str) -> float:
        start = self._active_starts.pop(key, None)
        if start is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._timers[key] = self._timers.get(key, 0.0) + elapsed_ms
            return elapsed_ms
        return 0.0

    def add_timing(self, key: str, duration_ms: float):
        self._timers[key] = self._timers.get(key, 0.0) + duration_ms

    def increment(self, counter_key: str, count: int = 1):
        self._counters[counter_key] = self._counters.get(counter_key, 0) + count

    def finish(self) -> Dict[str, Any]:
        if self._end_time is None:
            self._end_time = time.perf_counter()

        total_seconds = max(0.001, self._end_time - self._start_time)
        avg_per_page_seconds = total_seconds / self.total_pages

        return {
            'total_pages': self.total_pages,
            'total_time_seconds': round(total_seconds, 2),
            'avg_time_per_page_seconds': round(avg_per_page_seconds, 3),
            'timings_ms': {k: round(v, 2) for k, v in self._timers.items()},
            'counters': dict(self._counters),
        }
