"""
Wholesale AI — Main Entry Point.
Run the dashboard server + pipeline together.

Usage:
    python main.py              # Run dashboard + pipeline
    python main.py --server     # Dashboard only
    python main.py --pipeline   # Pipeline only
"""
import sys
import asyncio
import threading
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("wholesale.log"),
    ],
)
logger = logging.getLogger("wholesale-ai")


def run_server():
    """Start the FastAPI dashboard."""
    import uvicorn
    from config.settings import settings
    uvicorn.run(
        "dashboard.app:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
    )


def run_pipeline():
    """Start the 24/7 pipeline in its own event loop."""
    from pipeline import WholesalePipeline
    pipeline = WholesalePipeline()
    asyncio.run(pipeline.run_forever())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--both"

    if mode == "--server":
        logger.info("Starting dashboard server only...")
        run_server()
    elif mode == "--pipeline":
        logger.info("Starting pipeline only...")
        run_pipeline()
    else:
        logger.info("Starting Wholesale AI (dashboard + pipeline)...")
        # Run pipeline in background thread
        pipeline_thread = threading.Thread(target=run_pipeline, daemon=True)
        pipeline_thread.start()
        # Run server in main thread
        run_server()


if __name__ == "__main__":
    main()
