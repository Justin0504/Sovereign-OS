"""Job ingestion: poll external sources and enqueue jobs."""

from sovereign_os.ingest.poller import start_ingest_poller

__all__ = ["start_ingest_poller"]
