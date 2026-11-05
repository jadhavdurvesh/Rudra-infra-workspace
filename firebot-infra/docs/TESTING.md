# Testing

    python -m unittest discover -s tests          # everything (needs ~30 s)
    firebot-infra selftest                         # 15-check smoke test you can run on the robot computer
Install `.[crypto]` to include the KAVACH, TLS and signed-message tests; without it those files are skipped with a clear message.

## Dossier fault list -> where it is tested
| Fault (Paper 11) | Test(s) |
|---|---|
| Network disconnection | `test_setu` (disconnect/recover), `test_fault_injection.NetworkTests` (real peer process killed and restarted over TCP), `test_jala` (partitions) |
| Packet loss | `test_setu`, `test_spanda` (20% loss + reorder + corruption), `test_jala`, `test_kavach_envelope` (lost reply with retry) |
| Duplicate messages | `test_setu`, `test_kavach_envelope` (replay), `test_sangam`, `test_ananta` |
| Slow consumers | `test_setu` (bounded inbox), `test_akshara`, `test_ananta`, `test_samaya` (bounded buffer) |
| Process crashes | `test_fault_injection.ProcessCrashTests`: SIGKILL of recorder, DHARA, SANGAM and PRAVAH processes; `test_dhara` (crash at every byte offset) |
| Disk full | `test_fault_injection.DiskFullTests`: real OS file-size limit against recorder, spool and DHARA |
| Corrupted records | `test_akshara`, `test_lipi`, `test_dhara` (every single-byte flip), `test_fault_injection.CorruptionTests` |
| Stale sensor values | `test_drishti`, `test_tantu`, flatline tests |
| Expired deadlines / unsupported schemas | `test_setu`, `test_kavach_envelope` |

## Guarantees the fault tests establish
- A process killed at any moment leaves a recording that reads back as a clean prefix, honestly marked as not cleanly closed.
- A failed write (disk full) never leaves half a record: the file is cut back to the last whole record, the failure is counted
  and visible in the manifest, and the caller never sees an exception from the recorder or spool. DHARA reports the failure to its
  caller and never loses a write it acknowledged, nor resurrects one it reported as failed.
- Unsafe-to-repeat tasks are never re-run after a restart; safe ones are.

## Limits of this evidence
Everything runs in software on a development machine. Nothing here tests real sensors, radios, power loss mid-fsync on real
flash storage, or electrical faults. The record buffer in the OS can still be lost on power failure between fsync intervals.
