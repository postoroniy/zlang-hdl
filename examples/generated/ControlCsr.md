# ControlCsr CSR map

Bus width: 32 bits.

## control

Base address: `0x40000000`

### CONTROL

Offset `0x00`, address `0x40000000`.

| Field | Bits | Type | Access | Reset |
|---|---:|---|---|---:|
| enable | 0 | `bit` | `rw` | `0x0` |
| mode | 3:1 | `u3` | `rw` | `0x0` |
| start | 4 | `bit` | `pulse` | `0x0` |
| command | 7:5 | `u3` | `wo` | `0x0` |
| reserved0 | 31:8 | `bits<24>` | `reserved` | `0x0` |

### STATUS

Offset `0x04`, address `0x40000004`.

| Field | Bits | Type | Access | Reset |
|---|---:|---|---|---:|
| busy | 0 | `bit` | `ro` | `0x1` |
| error | 1 | `bit` | `w1c` | `0x1` |
| reserved1 | 31:2 | `bits<30>` | `reserved` | `0x0` |
