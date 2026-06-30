import pytest
from pathlib import Path
from PIL import Image
import file_manager


class TestSanitizeName:
    def test_spaces_become_underscores(self):
        assert file_manager.sanitize_name('section cut') == 'section_cut'

    def test_multiple_spaces(self):
        assert file_manager.sanitize_name('left side view') == 'left_side_view'

    def test_forbidden_slash_stripped(self):
        assert file_manager.sanitize_name('front/back') == 'frontback'

    def test_forbidden_colon_stripped(self):
        assert file_manager.sanitize_name('view:1') == 'view1'

    def test_all_forbidden_chars_stripped(self):
        result = file_manager.sanitize_name(r'a\b:c*d?e"f<g>h|i')
        assert result == 'abcdefghi'

    def test_clean_name_unchanged(self):
        assert file_manager.sanitize_name('front') == 'front'

    def test_clean_name_with_underscore_unchanged(self):
        assert file_manager.sanitize_name('logo_area') == 'logo_area'


def _make_image(path: Path, width=100, height=80, color='red'):
    Image.new('RGB', (width, height), color=color).save(str(path))


class TestSaveCrop:
    def test_creates_output_folder(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert (tmp_path / 'photo_001').is_dir()

    def test_copies_original_into_folder(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert (tmp_path / 'photo_001' / 'photo_001.jpg').exists()

    def test_saves_crop_at_expected_path(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        crop_path = file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        assert crop_path == tmp_path / 'photo_001' / 'photo_001_front.jpg'
        assert crop_path.exists()

    def test_crop_has_correct_dimensions(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src, width=100, height=80)
        crop_path = file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        with Image.open(crop_path) as img:
            assert img.size == (50, 40)

    def test_sanitizes_name_in_filename(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        crop_path = file_manager.save_crop(str(src), (0, 0, 50, 40), 'section cut')
        assert crop_path.name == 'photo_001_section_cut.jpg'

    def test_does_not_recopy_original_on_second_save(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        file_manager.save_crop(str(src), (10, 10, 50, 40), 'front')
        mtime_after_first = (tmp_path / 'photo_001' / 'photo_001.jpg').stat().st_mtime
        file_manager.save_crop(str(src), (20, 20, 30, 30), 'back')
        assert (tmp_path / 'photo_001' / 'photo_001.jpg').stat().st_mtime == mtime_after_first

    def test_returns_path_object(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        result = file_manager.save_crop(str(src), (0, 0, 50, 40), 'front')
        assert isinstance(result, Path)

    def test_png_source_saves_as_png(self, tmp_path):
        src = tmp_path / 'photo_001.png'
        Image.new('RGB', (100, 80)).save(str(src))
        crop_path = file_manager.save_crop(str(src), (0, 0, 50, 40), 'front')
        assert crop_path.suffix == '.png'

    def test_raises_on_all_forbidden_name(self, tmp_path):
        src = tmp_path / 'photo_001.jpg'
        _make_image(src)
        with pytest.raises(ValueError, match='empty after sanitization'):
            file_manager.save_crop(str(src), (0, 0, 50, 40), '???')
