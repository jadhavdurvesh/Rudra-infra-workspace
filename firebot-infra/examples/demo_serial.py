"""Serial-link demo: TANTU reads a framed UART sensor -> DRISHTI; SETU talks over a framed link; TARANG decodes the capture.
Run: PYTHONPATH=src python examples/demo_serial.py"""
import struct, time
from firebot_infra.drishti import MeasurementSpec, StateEngine
from firebot_infra.framing import encode_frame
from firebot_infra.setu import Node, SchemaRegistry, StreamTransport, make_pipe
from firebot_infra.tantu import Binding, PeripheralRuntime, UartSensorDriver, struct_parser
from firebot_infra.tarang import Capture, SetuDecoder, decode_capture

# 1) a simulated temperature sensor streaming framed float32 readings over a "UART"
dev, host = make_pipe()
engine = StateEngine()
rt = PeripheralRuntime(engine)
spec = MeasurementSpec("temperature", "degC", max_age_s=1.0, min_value=-40, max_value=1200)
rt.bind(Binding("temp1", UartSensorDriver(host, "temperature", "degC", struct_parser(">f")), 0.05, spec))
rt.start()
for v in (21.0, 21.4, 22.1):
    dev.write(encode_frame(struct.pack(">f", v)))
    time.sleep(0.1)
print("DRISHTI temp:", engine.get("temp1", "temperature").status.value, round(engine.get("temp1", "temperature").value, 2))
print("TANTU health:", rt.health()["temp1"].state)
rt.stop()

# 2) SETU over a framed serial link, with a tap on the wire
a_s, b_s = make_pipe(); a_s.record = []
reg = SchemaRegistry(); reg.register("ping", 1, {"n": int})
a, b = Node("a", StreamTransport(a_s, "b"), reg), Node("b", StreamTransport(b_s, "a"), reg)
b.on("ping", lambda e: {"n": e.payload["n"] + 1})
print("SETU over serial:", a.request("b", "ping", {"n": 41}, timeout=2).payload)
a.close(); b.close()

# 3) TARANG decodes what was on the wire
rep = decode_capture(Capture.from_bytes(b"".join(a_s.record)), SetuDecoder())
print("TARANG:", [(f.decoded["message_type"], f.decoded["kind"]) for f in rep.frames], "clean =", rep.clean)
