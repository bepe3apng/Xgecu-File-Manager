import os
import struct

# Сигнатура заголовка Android Sparse Image: 0xED26FF3A
SPARSE_HEADER_MAGIC = 0xED26FF3A
SPARSE_CHUNK_RAW = 0xCAC1
SPARSE_CHUNK_FILL = 0xCAC2
SPARSE_CHUNK_DONT_CARE = 0xCAC3
SPARSE_CHUNK_CRC32 = 0xCAC4


class ImageProcessor:
    """Драйвер анализа и сегментации бинарных образов для оптимизированной записи."""

    def __init__(self, chunk_bytes=16384, min_skip_bytes=1024 * 1024):
        """
        :param chunk_bytes: размер единичной порции стриминга T48 (16 КБ = 32 сектора)
        :param min_skip_bytes: порог непрерывных нулей для разделения стрима (по умолчанию 1 МБ)
        """
        self.chunk_bytes = chunk_bytes
        self.min_skip_bytes = min_skip_bytes

    def analyze(self, file_path: str) -> dict:
        """
        Сканирует файл и формирует карту сегментов для записи.
        Возвращает словарь:
          - 'format': 'sparse' или 'raw'
          - 'total_bytes': исходный размер
          - 'data_bytes': фактический объем данных к записи
          - 'skip_bytes': сэкономленный объем
          - 'segments': список кортежей (lba_offset_sectors, file_seek_pos, length_bytes)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Файл не найден: {file_path}")

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return {
                "format": "empty",
                "total_bytes": 0,
                "data_bytes": 0,
                "skip_bytes": 0,
                "segments": []
            }

        with open(file_path, "rb") as f:
            magic = f.read(4)
            if len(magic) == 4 and struct.unpack("<I", magic)[0] == SPARSE_HEADER_MAGIC:
                f.seek(0)
                return self._parse_sparse(f, file_size)
            else:
                f.seek(0)
                return self._parse_raw(f, file_size)

    def _parse_raw(self, f, file_size: int) -> dict:
        """Keep RAW data intact; the writer skips fill only after device erase."""
        return {
            "format": "raw",
            "total_bytes": file_size,
            "data_bytes": file_size,
            "skip_bytes": 0,
            "segments": [(0, 0, file_size)] if file_size else [],
        }

    def _parse_sparse(self, f, file_size: int) -> dict:
        """Разбор карты чанков Android Sparse Image."""
        hdr_data = f.read(28)
        magic, major, minor, file_hdr_sz, chunk_hdr_sz, blk_sz, total_blks, total_chunks, _ = struct.unpack(
            "<IHHHHIIII", hdr_data
        )

        segments = []
        total_data = 0
        cur_lba = 0

        for _ in range(total_chunks):
            chunk_hdr = f.read(chunk_hdr_sz)
            if len(chunk_hdr) < 12:
                break
            chunk_type, _, chunk_sz_blks, total_sz_bytes = struct.unpack("<HHII", chunk_hdr[:12])
            chunk_data_sz = total_sz_bytes - chunk_hdr_sz
            data_file_pos = f.tell()

            sectors_count = (chunk_sz_blks * blk_sz) // 512

            if chunk_type == SPARSE_CHUNK_RAW:
                segments.append((cur_lba, data_file_pos, chunk_data_sz))
                total_data += chunk_data_sz
                f.seek(data_file_pos + chunk_data_sz)
            elif chunk_type in (SPARSE_CHUNK_DONT_CARE, SPARSE_CHUNK_FILL):
                # Блоки заполнения и пропуска не читаем с диска
                if chunk_data_sz > 0:
                    f.seek(data_file_pos + chunk_data_sz)
            elif chunk_type == SPARSE_CHUNK_CRC32:
                f.seek(data_file_pos + chunk_data_sz)

            cur_lba += sectors_count

        return {
            "format": "sparse",
            "total_bytes": total_blks * blk_sz,
            "data_bytes": total_data,
            "skip_bytes": (total_blks * blk_sz) - total_data,
            "segments": segments
        }