import threading
import unittest

from firebot_infra.drishti import MeasurementSpec, StateEngine, Status
from firebot_infra.setu import make_pipe
from firebot_infra.setu_edge import (ModbusClient, ModbusError, ModbusException, ModbusSensorDriver, ModbusServer,
                                     ModbusTimeout, RtuStreamLink, ServerLink, WriteDenied, WriteDisabled, crc16,
                                     parse_response, rtu_frame, rtu_parse, serve_rtu_stream, tcp_frame, tcp_parse)
from firebot_infra.tantu import DRIVERS, Binding, DriverError, PeripheralRuntime, build_bindings


def rig(mode="rtu", **kw):
    srv = ModbusServer(1, coils=32, discrete=16, holding=20, input_regs=20)
    link = ServerLink(srv, mode)
    return srv, link, ModbusClient(link, 1, mode, timeout=0.05, **kw)


class CodecTests(unittest.TestCase):
    def test_crc_known_vector(self):
        self.assertEqual(crc16(bytes.fromhex("01030000000A")), 0xCDC5)
        self.assertEqual(rtu_frame(1, bytes.fromhex("03 00 00 00 0A")).hex(), "01030000000ac5cd")

    def test_frames_roundtrip_and_reject(self):
        f = rtu_frame(7, b"\x03\x00\x01\x00\x02")
        self.assertEqual(rtu_parse(f), (7, b"\x03\x00\x01\x00\x02"))
        with self.assertRaises(ModbusError):
            rtu_parse(f[:-1] + b"\x00")
        with self.assertRaises(ModbusError):
            rtu_parse(b"\x01")
        t = tcp_frame(0x1234, 5, b"\x03\x00\x00\x00\x01")
        self.assertEqual(tcp_parse(t), (0x1234, 5, b"\x03\x00\x00\x00\x01"))
        with self.assertRaises(ModbusError):
            tcp_parse(t[:-1])

    def test_parse_response_is_robust_to_garbage(self):
        req = bytes.fromhex("03 0000 0002")
        for bad in (b"", b"\x03", b"\x03\x04\x00\x01", b"\x04\x04\x00\x01\x00\x02", b"\x03\x02\x00\x01"):
            with self.assertRaises(ModbusError):
                parse_response(req, bad)
        with self.assertRaises(ModbusException) as c:
            parse_response(req, b"\x83\x02")
        self.assertEqual(c.exception.code, 2)


class ClientServerTests(unittest.TestCase):
    def test_all_functions_both_modes(self):
        for mode in ("rtu", "tcp"):
            srv, link, c = rig(mode, allow_write=True)
            self.assertTrue(c.write_multiple_registers(2, [10, 20, 65535]))
            self.assertEqual(c.read_holding_registers(2, 3), [10, 20, 65535])
            self.assertTrue(c.write_single_register(0, 99))
            self.assertEqual(srv.holding[:1], [99])
            self.assertTrue(c.write_multiple_coils(3, [True, False, True, True, False, False, False, False, True]))
            self.assertEqual(c.read_coils(3, 9), [True, False, True, True, False, False, False, False, True])
            self.assertTrue(c.write_single_coil(0, True))
            self.assertEqual(c.read_coils(0, 1), [True])
            srv.input[4:6] = [111, 222]; srv.discrete[1] = True
            self.assertEqual(c.read_input_registers(4, 2), [111, 222])
            self.assertEqual(c.read_discrete_inputs(0, 3), [False, True, False])

    def test_device_exceptions_not_retried(self):
        srv, link, c = rig()
        with self.assertRaises(ModbusException) as e:
            c.read_holding_registers(19, 5)                 # beyond the register map
        self.assertEqual(e.exception.code, 2)
        self.assertEqual(link.calls, 1)
        srv2, link2, c2 = rig(allow_write=True)
        with self.assertRaises(ModbusException) as e:
            c2.write_single_register(500, 1)
        self.assertEqual(e.exception.code, 2)

    def test_reads_retry_on_timeout_and_corruption(self):
        srv, link, c = rig()
        srv.holding[0] = 42
        link.drop = 1
        self.assertEqual(c.read_holding_registers(0, 1), [42])
        link.corrupt = 2
        self.assertEqual(c.read_holding_registers(0, 1), [42])
        self.assertGreaterEqual(c.stats["retries"], 3)
        link.drop = 10
        with self.assertRaises(ModbusTimeout):
            c.read_holding_registers(0, 1)

    def test_read_only_by_default_and_writes_never_retried(self):
        srv, link, c = rig()
        with self.assertRaises(WriteDisabled):
            c.write_single_register(0, 1)
        self.assertEqual(link.calls, 0)
        srv, link, c = rig(allow_write=True)
        link.drop = 1
        with self.assertRaises(ModbusTimeout):
            c.write_single_register(0, 1)
        self.assertEqual(link.calls, 1)                    # no automatic retry of a side-effecting request

    def test_write_guard(self):
        seen = []
        srv, link, c = rig(allow_write=True, write_guard=lambda fc, addr, v: seen.append((fc, addr, v)) or addr < 5)
        c.write_single_register(2, 7)
        with self.assertRaises(WriteDenied):
            c.write_multiple_registers(10, [1, 2])
        self.assertEqual(srv.holding[10:12], [0, 0])
        self.assertEqual(seen, [(6, 2, 7), (16, 10, [1, 2])])

    def test_argument_validation(self):
        srv, link, c = rig(allow_write=True)
        for call in (lambda: c.read_holding_registers(0, 0), lambda: c.read_holding_registers(0, 126),
                     lambda: c.read_coils(0, 2001), lambda: c.write_single_register(0, 70000),
                     lambda: c.read_holding_registers(65535, 2), lambda: c.write_multiple_registers(0, [])):
            with self.assertRaises(ValueError):
                call()
        with self.assertRaises(ValueError):
            ModbusClient(link, mode="ascii")

    def test_wrong_unit_ignored_and_tcp_tid_checked(self):
        srv, link, c = rig()
        c2 = ModbusClient(link, unit_id=9, timeout=0.05, retries=0)
        with self.assertRaises(ModbusTimeout):
            c2.read_holding_registers(0, 1)                 # server stays silent for other unit ids
        srv, link, c = rig("tcp")
        real = link.server.handle_tcp
        link.server.handle_tcp = lambda f: (lambda r: r[:0] + b"\xff\xff" + r[2:])(real(f))   # wrong tid in reply
        with self.assertRaises(ModbusError):
            c.read_holding_registers(0, 1)

    def test_server_rejects_malformed_requests(self):
        srv = ModbusServer(1, holding=5, coils=5)
        for pdu, code in ((b"\x2a\x00\x00", 1), (b"\x03\x00", 3), (b"\x03\x00\x00\x00\x00", 3),
                          (b"\x05\x00\x00\x12\x34", 3), (b"\x10\x00\x00\x00\x02\x04\x00\x01", 3)):
            self.assertEqual(srv.handle_pdu(pdu)[1], code, pdu.hex())
        self.assertIsNone(srv.handle_rtu(b"\x01\x03\x00\x00\x00\x01\x00\x00"))   # bad crc: silence


class StreamAndDriverTests(unittest.TestCase):
    def test_rtu_over_byte_stream(self):
        a, b = make_pipe()
        srv = ModbusServer(1, holding=10, input_regs=10)
        srv.holding[3] = 1234
        stop = threading.Event()
        serve_rtu_stream(srv, b, stop)
        try:
            c = ModbusClient(RtuStreamLink(a), 1, "rtu", timeout=1.0, allow_write=True)
            self.assertEqual(c.read_holding_registers(3, 1), [1234])
            c.write_multiple_registers(0, [5, 6, 7])
            self.assertEqual(srv.holding[:3], [5, 6, 7])
            with self.assertRaises(ModbusException):
                c.read_holding_registers(9, 5)
        finally:
            stop.set(); a.close(); b.close()

    def test_driver_decodes_scales_and_feeds_drishti(self):
        import struct
        srv, link, c = rig()
        srv.holding[0] = 215                                           # 21.5 degC at scale 0.1
        srv.holding[1] = (-300) & 0xFFFF                               # i16
        hi, lo = struct.unpack(">HH", struct.pack(">f", 3.25))
        srv.input[0], srv.input[1] = hi, lo
        srv.holding[4], srv.holding[5] = 0x0001, 0x0002               # u32 = 65538 (big word order)
        eng = StateEngine()
        eng.register(MeasurementSpec("temperature", "degC", max_age_s=5, min_value=-40, max_value=200))
        eng.register(MeasurementSpec("level", "m", max_age_s=5))
        rt = PeripheralRuntime(eng)
        rt.bind(Binding("t", ModbusSensorDriver(c, 0, "temperature", "degC", scale=0.1), 1))
        rt.bind(Binding("i", ModbusSensorDriver(c, 1, "temperature", "degC", data_type="i16", scale=0.1), 1))
        rt.bind(Binding("f", ModbusSensorDriver(c, 0, "level", "m", kind="input", data_type="f32"), 1))
        rt.bind(Binding("u", ModbusSensorDriver(c, 4, "level", "m", data_type="u32"), 1))
        rt.bind(Binding("w", ModbusSensorDriver(c, 4, "level", "m", data_type="u32", word_order="little"), 1))
        for s in "tifuw":
            self.assertTrue(rt.poll(s), s)
        self.assertAlmostEqual(eng.get("t", "temperature").value, 21.5)
        self.assertAlmostEqual(eng.get("i", "temperature").value, -30.0)
        self.assertAlmostEqual(eng.get("f", "level").value, 3.25)
        self.assertEqual(eng.get("u", "level").value, 65538.0)
        self.assertEqual(eng.get("w", "level").value, 131073.0)

    def test_driver_maps_bus_failures_to_driver_errors_and_state_goes_stale(self):
        srv, link, c = rig()
        link.drop = 100
        d = ModbusSensorDriver(c, 0, "temperature", "degC")
        with self.assertRaises(DriverError):
            d.read()
        eng = StateEngine()
        eng.register(MeasurementSpec("temperature", "degC", max_age_s=1))
        rt = PeripheralRuntime(eng)
        rt.bind(Binding("t", d, 1))
        self.assertFalse(rt.poll("t"))
        self.assertEqual(eng.get("t", "temperature").status, Status.UNAVAILABLE)

    def test_driver_is_registered_for_config_building(self):
        self.assertIn("modbus", DRIVERS)
        srv, link, c = rig()
        b = build_bindings([{"sensor_id": "m1", "driver": "modbus", "period_s": 1,
                             "params": {"client": "@bus", "address": 0, "measurement_type": "temperature",
                                        "unit": "degC", "scale": 0.1},
                             "spec": {"measurement_type": "temperature", "unit": "degC", "max_age_s": 5}}],
                           {"bus": c})
        self.assertIsInstance(b[0].driver, ModbusSensorDriver)

    def test_bad_driver_config(self):
        srv, link, c = rig()
        for kw in ({"data_type": "u64"}, {"kind": "coil"}, {"word_order": "mixed"}):
            with self.assertRaises(ValueError):
                ModbusSensorDriver(c, 0, "t", "u", **kw)


if __name__ == "__main__":
    unittest.main()
