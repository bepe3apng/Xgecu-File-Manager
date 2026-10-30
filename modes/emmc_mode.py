"""Фасад драйвера eMMC.

Вся логика теперь разложена по слоям пакета modes.emmc
(protocol / registers / partitions / session / operations),
а этот класс остаётся стабильным внешним API для оркестратора.
"""
from modes.emmc import operations


class EmmcDriver:
    """Совместимый фасад: та же сигнатура методов, что и раньше."""

    def __init__(self, log_cb=print):
        self.log = log_cb

    def write_extcsd(self, is_18v, bus_width, index, value, expected_data,
                     expected_cid, abort_check=None, allow_otp=False):
        return operations.write_extcsd(
            self.log, is_18v, bus_width, index, value, expected_data,
            expected_cid, abort_check, allow_otp)

    def read_extcsd(self, is_18v, bus_width, abort_check=None):
        return operations.read_extcsd(self.log, is_18v, bus_width, abort_check)

    def read_info_and_partitions(self, is_18v, bus_width, abort_check=None):
        return operations.read_info_and_partitions(
            self.log, is_18v, bus_width, abort_check)

    def dump_boot(self, is_18v, bus_width, part_name, size_bytes, out_path,
                  abort_check=None, verify=False):
        return operations.dump_boot(
            self.log, is_18v, bus_width, part_name, size_bytes, out_path, abort_check, verify=verify)

    def dump_user_area(self, is_18v, bus_width, out_path, abort_check=None, verify=False):
        return operations.dump_user_area(self.log, is_18v, bus_width, out_path, abort_check, verify=verify)

    def dump_lba_range(self, is_18v, bus_width, part_name, start_lba, end_lba,
                       out_path, abort_check=None, verify=False):
        operations.dump_lba_range(
            self.log, is_18v, bus_width, part_name, start_lba, end_lba,
            out_path, abort_check, verify=verify)

    def erase_blocks(self, is_18v, bus_width, part_type, start_lba, end_lba,
                     abort_check=None):
        operations.erase_blocks(
            self.log, is_18v, bus_width, part_type, start_lba, end_lba, abort_check)

    def write_partition(self, is_18v, bus_width, part_name, in_path, abort_check=None, verify=False, erase_before=False):
        return operations.write_partition(
            self.log, is_18v, bus_width, part_name, in_path, abort_check, verify=verify, erase_before=erase_before)

    def write_lba_range(self, is_18v, bus_width, part_name, start_lba, end_lba,
                        in_path, abort_check=None, verify=False, erase_before=False):
        operations.write_lba_range(
            self.log, is_18v, bus_width, part_name, start_lba, end_lba,
            in_path, abort_check, verify=verify, erase_before=erase_before)
