#!/usr/bin/env python3
"""
FAST parallel USDA PDF analyzer - processes local PDFs with concurrent workers.

Key optimizations:
1. Parallel processing (8+ workers)
2. No network delays (reads local PDFs)
3. Batch database writes
4. Progress tracking with ETA
5. Automatic resume from checkpoint

Usage:
  python3 analyze_usda_parallel.py                    # Auto-detect and run
  python3 analyze_usda_parallel.py --workers 16       # Use 16 parallel workers
  python3 analyze_usda_parallel.py --batch-size 50    # Write DB in batches of 50
  python3 analyze_usda_parallel.py --fast             # Use faster LLM (gpt-4o-mini)
"""
import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from typing import List, Dict, Any
import multiprocessing

# Environment setup
# Connection details default to local dev / Cloud SQL convention. Real
# credentials must come from the operator's environment (.env, direnv,
# Secret Manager pull, etc.). Refuse to start if DB_PASSWORD is unset.
os.environ.setdefault('CLOUD_SQL_CONNECTION_NAME', 'formgenome:us-central1:formgenome-db')
os.environ.setdefault('DB_NAME', 'postgres')
os.environ.setdefault('DB_USER', 'postgres')
os.environ.setdefault('FLASK_ENV', 'development')
if not os.environ.get('DB_PASSWORD'):
    raise RuntimeError(
        "DB_PASSWORD must be set in the environment before running "
        "analyze_usda_parallel.py. Use: export DB_PASSWORD=... or source a .env file."
    )
if not os.environ.get('SECRET_KEY'):
    raise RuntimeError("SECRET_KEY must be set in the environment.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configuration
PDF_DIR = Path("usda_pdfs")
CHECKPOINT_FILE = "usda_parallel_checkpoint.json"
REPORT_TEMPLATE = "ui/report-template.html"

class ParallelAnalyzer:
    def __init__(self, workers=None, batch_size=20, fast_mode=False):
        self.workers = workers or max(8, multiprocessing.cpu_count())
        self.batch_size = batch_size
        self.fast_mode = fast_mode
        self.checkpoint_path = Path(CHECKPOINT_FILE)
        self.checkpoint = self.load_checkpoint()

        # Set LLM model based on mode
        if fast_mode:
            os.environ['LLM_PROVIDER'] = 'openai'
            os.environ['OPENAI_MODEL'] = 'gpt-4o-mini'  # Faster, cheaper
            print(f"🚀 FAST MODE: Using gpt-4o-mini")
        else:
            os.environ['LLM_PROVIDER'] = 'openai'
            os.environ['OPENAI_MODEL'] = 'gpt-4o'
            print(f"🔬 QUALITY MODE: Using gpt-4o")

    def load_checkpoint(self):
        """Load checkpoint file if it exists"""
        if self.checkpoint_path.exists():
            with open(self.checkpoint_path, 'r') as f:
                return json.load(f)
        return {
            'completed_files': [],
            'failed_files': [],
            'form_ids': [],
            'started_at': None,
            'last_update': None,
            'total_files': 0
        }

    def save_checkpoint(self):
        """Save checkpoint to disk"""
        self.checkpoint['last_update'] = datetime.now().isoformat()
        with open(self.checkpoint_path, 'w') as f:
            json.dump(self.checkpoint, f, indent=2)

    def find_local_pdfs(self) -> List[Path]:
        """Find all PDFs in the local directory"""
        if not PDF_DIR.exists():
            print(f"✗ PDF directory not found: {PDF_DIR}")
            return []

        pdfs = list(PDF_DIR.glob("*.pdf"))
        print(f"📁 Found {len(pdfs)} PDFs in {PDF_DIR}")
        return pdfs

    def analyze_single_pdf(self, pdf_path: Path) -> Dict[str, Any]:
        """
        Analyze a single PDF file (runs in worker process).
        This function must be importable and picklable for multiprocessing.
        """
        # Import here to avoid issues with multiprocessing
        from app.services import analyzer, storage

        try:
            # Convert to absolute path and then to file:// URL for local processing
            abs_path = pdf_path.resolve()
            file_url = abs_path.as_uri()

            # Analyze the PDF
            record = analyzer.analyze_pdf(
                pdf_url=file_url,
                timeout=45,
                disable_size_guard=True,  # Local files are safe
                max_pdf_mb=120,
                force_minimal=False,  # Get full analysis
                skip_vision=False,
                skip_llm_title=False
            )

            if record and isinstance(record, dict):
                # Calculate quality
                confidence_tier, confidence_score, quality_signals = analyzer.calculate_quality_score(record)

                # Add metadata
                record["committed"] = True
                record["confidence_tier"] = confidence_tier
                record["confidence_score"] = confidence_score
                record["quality_signals"] = quality_signals
                record["entity_name"] = "USDA"
                record["industry_vertical"] = "Public Sector"
                record["industry_subvertical"] = "Federal Government"
                record["_local_file"] = str(pdf_path.name)

                return {
                    'success': True,
                    'file': str(pdf_path.name),
                    'record': record,
                    'complexity': record.get('complexity_score', 0),
                    'nigo': record.get('nigo_score', 0),
                    'tier': confidence_tier
                }
            else:
                return {
                    'success': False,
                    'file': str(pdf_path.name),
                    'error': 'No record returned'
                }

        except Exception as e:
            return {
                'success': False,
                'file': str(pdf_path.name),
                'error': str(e)[:200]
            }

    def batch_save_to_db(self, records: List[Dict[str, Any]]) -> int:
        """Save multiple records to database in a batch"""
        from app.services import storage

        saved_count = 0
        try:
            storage.init_db()

            for record in records:
                try:
                    record_id = storage.save(record)
                    if record_id:
                        self.checkpoint['form_ids'].append(record_id)
                        saved_count += 1
                except Exception as e:
                    print(f"  ⚠ Save failed for {record.get('form_name', 'unknown')}: {e}")

        except Exception as e:
            print(f"  ✗ Database batch save error: {e}")

        return saved_count

    def analyze_all(self):
        """Main analysis loop with parallel processing"""
        print("\n" + "="*80)
        print(f"  🧬 PARALLEL USDA ANALYZER ({self.workers} workers)")
        print("="*80 + "\n")

        # Find all PDFs
        all_pdfs = self.find_local_pdfs()
        if not all_pdfs:
            print("✗ No PDFs found")
            return

        # Filter out already completed
        completed_set = set(self.checkpoint.get('completed_files', []))
        failed_set = set(self.checkpoint.get('failed_files', []))
        remaining_pdfs = [p for p in all_pdfs if p.name not in completed_set and p.name not in failed_set]

        total = len(all_pdfs)
        completed_count = len(completed_set)
        failed_count = len(failed_set)
        remaining_count = len(remaining_pdfs)

        print(f"Progress: {completed_count} completed, {failed_count} failed, {remaining_count} remaining\n")

        if remaining_count == 0:
            print("✓ All PDFs already processed!")
            self.generate_report()
            return

        # Initialize checkpoint
        if not self.checkpoint.get('started_at'):
            self.checkpoint['started_at'] = datetime.now().isoformat()
            self.checkpoint['total_files'] = total

        # Batch processing variables
        pending_records = []
        start_time = time.time()
        processed = 0

        print(f"🚀 Starting parallel analysis with {self.workers} workers...\n")

        # Process PDFs in parallel
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            # Submit all tasks
            future_to_pdf = {
                executor.submit(self.analyze_single_pdf, pdf): pdf
                for pdf in remaining_pdfs
            }

            # Process completed tasks as they finish
            for future in as_completed(future_to_pdf):
                pdf = future_to_pdf[future]
                processed += 1
                current_total = completed_count + failed_count + processed

                try:
                    result = future.result(timeout=60)

                    if result['success']:
                        # Add to pending batch
                        pending_records.append(result['record'])
                        self.checkpoint['completed_files'].append(result['file'])

                        # Calculate progress stats
                        elapsed = time.time() - start_time
                        rate = processed / elapsed if elapsed > 0 else 0
                        eta_seconds = (remaining_count - processed) / rate if rate > 0 else 0
                        eta_mins = eta_seconds / 60

                        print(f"[{current_total}/{total}] ✓ {result['file'][:50]} "
                              f"(C:{result['complexity']:.0f}, N:{result['nigo']:.0f}, {result['tier']}) "
                              f"[{rate:.1f}/s, ETA: {eta_mins:.1f}m]")

                        # Batch save when we hit batch_size
                        if len(pending_records) >= self.batch_size:
                            saved = self.batch_save_to_db(pending_records)
                            print(f"  💾 Batch saved {saved} records to database")
                            pending_records = []
                            self.save_checkpoint()
                    else:
                        self.checkpoint['failed_files'].append(result['file'])
                        print(f"[{current_total}/{total}] ✗ {result['file'][:50]} - {result['error'][:80]}")
                        self.save_checkpoint()

                except Exception as e:
                    self.checkpoint['failed_files'].append(pdf.name)
                    print(f"[{current_total}/{total}] ✗ {pdf.name[:50]} - Exception: {str(e)[:80]}")
                    self.save_checkpoint()

        # Save any remaining records
        if pending_records:
            print(f"\n💾 Saving final batch of {len(pending_records)} records...")
            saved = self.batch_save_to_db(pending_records)
            print(f"  ✓ Saved {saved} records")
            self.save_checkpoint()

        # Summary
        elapsed_total = time.time() - start_time
        print("\n" + "="*80)
        print("  ANALYSIS COMPLETE")
        print("="*80)
        print(f"✓ Completed: {len(self.checkpoint['completed_files'])}")
        print(f"✗ Failed: {len(self.checkpoint['failed_files'])}")
        print(f"💾 Total saved: {len(self.checkpoint['form_ids'])}")
        print(f"⏱  Time: {elapsed_total/60:.1f} minutes ({elapsed_total/total:.1f}s per form)")
        print(f"📊 Rate: {total/elapsed_total:.2f} forms/second")

        # Generate report
        self.generate_report()

    def generate_report(self):
        """Generate HTML report from analyzed forms"""
        print("\n" + "="*80)
        print("  GENERATING REPORT")
        print("="*80 + "\n")

        from app.services import storage

        try:
            storage.init_db()

            # Load USDA forms from database
            records = storage.list_filtered(
                committed=True,
                industry_vertical="Public Sector"
            )
            records = [r for r in records if r.get('entity_name') == 'USDA']

            print(f"✓ Loaded {len(records)} USDA forms from database\n")

            if len(records) == 0:
                print("⚠ No USDA forms found in database")
                return

            # Generate report
            with open(REPORT_TEMPLATE, 'r') as f:
                template = f.read()

            data_script = f'<script>window.__REPORT_ROWS__={json.dumps(records, ensure_ascii=False)};</script>'

            if "<!--__DATA__-->" in template:
                html = template.replace("<!--__DATA__-->", data_script)
            else:
                html = template + "\n" + data_script

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"USDA_Parallel_Analysis_{timestamp}.html"

            with open(output_file, 'w') as f:
                f.write(html)

            print(f"✓ Report saved: {output_file}")
            print(f"\n🌐 Open in browser:")
            print(f"  file://{os.path.abspath(output_file)}\n")

        except Exception as e:
            print(f"✗ Report generation failed: {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Fast parallel USDA PDF analyzer')
    parser.add_argument('--workers', type=int, help='Number of parallel workers (default: CPU count)')
    parser.add_argument('--batch-size', type=int, default=20, help='Database batch size (default: 20)')
    parser.add_argument('--fast', action='store_true', help='Use faster LLM model (gpt-4o-mini)')
    parser.add_argument('--report-only', action='store_true', help='Generate report only')

    args = parser.parse_args()

    analyzer = ParallelAnalyzer(
        workers=args.workers,
        batch_size=args.batch_size,
        fast_mode=args.fast
    )

    if args.report_only:
        analyzer.generate_report()
    else:
        analyzer.analyze_all()

    print("\n✅ All done!")
    print(f"Checkpoint: {CHECKPOINT_FILE}")
    print(f"Resume anytime by running: python3 {sys.argv[0]}\n")


if __name__ == '__main__':
    main()
