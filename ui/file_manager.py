import re
import shutil
from pathlib import Path
from PIL import Image

FORBIDDEN = re.compile(r'[/\\:*?"<>|]')


def pdf_page_to_png(pdf_path: str, page: int = 0, dpi: int = 150) -> Path:
    """Render one page of a PDF to a PNG saved alongside the PDF. Returns the PNG path."""
    import fitz
    source = Path(pdf_path)
    doc = fitz.open(str(source))
    if page >= len(doc):
        raise ValueError(f"PDF has {len(doc)} page(s); page {page} does not exist")
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page].get_pixmap(matrix=mat, alpha=False)
    suffix = f'_page{page + 1}' if len(doc) > 1 else ''
    out_path = source.parent / f'{source.stem}{suffix}.png'
    pix.save(str(out_path))
    doc.close()
    return out_path


def save_feature_counts(source_path: str, features: dict) -> Path:
    """Write feature counts to [stem]_features.txt in the output folder."""
    source = Path(source_path)
    stem = source.stem
    folder = source.parent / stem
    folder.mkdir(exist_ok=True)
    out_path = folder / f'{stem}_features.txt'
    with open(out_path, 'w') as f:
        f.write(f'drawing: {stem}\n')
        for feature, count in features.items():
            f.write(f'{feature}: {count}\n')
    return out_path

def sanitize_name(name: str) -> str:
    name = FORBIDDEN.sub('', name)
    return name.replace(' ', '_')

def save_crop(source_path: str, rect: tuple, name: str) -> Path:
    source = Path(source_path)
    stem = source.stem
    ext = source.suffix
    folder = source.parent / stem
    folder.mkdir(exist_ok=True)

    original_dest = folder / source.name
    if not original_dest.exists():
        shutil.copy2(source, original_dest)

    safe_name = sanitize_name(name)
    if not safe_name:
        raise ValueError(f"name {name!r} is empty after sanitization")
    crop_path = folder / f'{stem}_{safe_name}{ext}'

    with Image.open(source_path) as img:
        x, y, w, h = rect
        cropped = img.crop((x, y, x + w, y + h))
        if ext.lower() in ('.jpg', '.jpeg') and cropped.mode in ('RGBA', 'P', 'LA'):
            cropped = cropped.convert('RGB')
        cropped.save(str(crop_path))

    return crop_path
