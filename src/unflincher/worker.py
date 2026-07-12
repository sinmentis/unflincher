"""In-process asyncio batch worker for 'apply to all'. Bounded concurrency via a semaphore;
per-item failure isolation; every successful item is persisted atomically (see
db.complete_job_item) so a crash mid-item can never produce a duplicate on resume."""
import asyncio

from unflincher import llm
from unflincher.db import complete_job_item, fail_job_item


class BatchWorker:
    def __init__(self, conn, concurrency: int = 3):
        self.conn = conn
        self.semaphore = asyncio.Semaphore(concurrency)

    async def run_job(self, job_id: int, persona_text: str, model: str) -> None:
        job = self.conn.execute("SELECT * FROM regen_job WHERE id = ?", (job_id,)).fetchone()
        tasks = []
        while True:
            item = self._claim_next_pending(job_id)
            if item is None:
                break
            tasks.append(asyncio.create_task(self._process_item(item, job["prompt_version_id"], persona_text, model)))
        if tasks:
            await asyncio.gather(*tasks)
        self._finalize_job(job_id)

    def _claim_next_pending(self, job_id):
        row = self.conn.execute(
            "SELECT * FROM regen_job_item WHERE job_id = ? AND status = 'pending' LIMIT 1",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE regen_job_item SET status = 'running', updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        return row

    async def _process_item(self, item, prompt_version_id, persona_text, model):
        async with self.semaphore:
            try:
                if item["target_type"] == "entry_commentary":
                    await self._generate_entry_commentary(item, prompt_version_id, persona_text, model)
                else:
                    await self._generate_aggregate_report(item, prompt_version_id, persona_text, model)
            except Exception as exc:
                fail_job_item(self.conn, item["id"], str(exc))

    async def _generate_entry_commentary(self, item, prompt_version_id, persona_text, model):
        entry = self.conn.execute("SELECT * FROM diary_entry WHERE id = ?", (item["entry_id"],)).fetchone()
        all_entries = self.conn.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
        chunks = [tok async for tok in llm.generate_commentary(dict(entry), [dict(e) for e in all_entries], persona_text, model)]
        complete_job_item(self.conn, item["id"], "entry_commentary", {
            "entry_id": item["entry_id"], "prompt_version_id": prompt_version_id,
            "model": model, "body_text": "".join(chunks), "status": "ok",
        })

    async def _generate_aggregate_report(self, item, prompt_version_id, persona_text, model):
        all_entries = self.conn.execute("SELECT * FROM diary_entry ORDER BY entry_date").fetchall()
        chunks = [tok async for tok in llm.generate_report([dict(e) for e in all_entries], persona_text, model)]
        dates = [e["entry_date"] for e in all_entries]
        complete_job_item(self.conn, item["id"], "aggregate_report", {
            "prompt_version_id": prompt_version_id, "model": model, "body_text": "".join(chunks),
            "covered_entry_count": len(all_entries),
            "covered_from_date": min(dates) if dates else None,
            "covered_to_date": max(dates) if dates else None,
            "status": "ok",
        })

    def _finalize_job(self, job_id):
        remaining = self.conn.execute(
            "SELECT COUNT(*) AS n FROM regen_job_item WHERE job_id = ? AND status IN ('pending','running')",
            (job_id,),
        ).fetchone()["n"]
        if remaining == 0:
            self.conn.execute(
                "UPDATE regen_job SET status = 'done', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
