import argparse
from pathlib import Path
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup
import requests


BASE_URL = "https://books.toscrape.com/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}


def _safe_filename(value: str) -> str:
    name = "".join(c for c in value if c.isalnum() or c in (" ", ".", "_")).rstrip()
    return name[:50] or "image"


def download_book_images(output_dir: Path, page: int = 1, delay: float = 0.5) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    catalogue_url = urljoin(BASE_URL, f"catalogue/page-{page}.html")

    response = requests.get(catalogue_url, headers=HEADERS, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    books = soup.find_all("article", class_="product_pod")

    for index, book in enumerate(books, start=1):
        img_tag = book.find("img")
        title_tag = book.find("h3").find("a")
        description = title_tag.get("title", title_tag.text.strip())
        img_url = urljoin(BASE_URL, img_tag.get("src"))

        img_response = requests.get(img_url, headers=HEADERS, timeout=10)
        img_response.raise_for_status()

        file_path = output_dir / f"{index}_{_safe_filename(description)}.jpg"
        file_path.write_bytes(img_response.content)
        time.sleep(delay)

    return len(books)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download sample book cover images.")
    parser.add_argument(
        "--output-dir",
        default="data/downloads/book_images",
        help="Directory for downloaded images.",
    )
    parser.add_argument("--page", type=int, default=1, help="Catalogue page number.")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between downloads.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    count = download_book_images(Path(args.output_dir), args.page, args.delay)
    print(f"Downloaded {count} images to {args.output_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
