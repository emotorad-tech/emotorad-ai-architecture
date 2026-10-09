# Amiigo Support Chat: dealer store cards

9 October 2026 · from the server team · addendum to v1. Adds one optional key to the bot's message object. Nothing is removed or renamed.

## What changes

When the bot sends the rider to a dealer, the bot's message carries `stores`: the three EMotorad dealer stores nearest the rider's pin code, nearest first. The app decides how many to show. The bot's text says the stores are listed below and never writes a phone number or an address itself.

`stores` appears on the bot message in the live `reply` frame (`message.stores`) and in `GET …/messages`, so a reopened chat redraws the cards. A message without stores has no `stores` key.

## The field

```json
"stores": [
  {"ref": "D1", "name": "Test Cycles Pune", "address": "Shop 1, Test Road, Viman Nagar, Pune, Maharashtra - 411014",
   "pincode": "411014", "manager_name": "Test Manager One", "phone": "+91 9000000001", "distance_km": 3}
]
```

| Field | Type | Meaning |
| --- | --- | --- |
| `ref` | string | `D1`, `D2`, `D3` in order. The bot may say "the first store". |
| `name` | string | The store's name |
| `address` | string | The full address, pin code included |
| `pincode` | string | The store's pin code |
| `manager_name` | string | The store manager, or the store's contact person |
| `phone` | string | `+91` and ten digits where the number has ten, else as stored |
| `distance_km` | number | Straight line from the rider's pin code to the store's, whole kilometres |

## How to show a card

Name and distance on the first line, the address below, then the manager's name and a call button on the phone. Keep the order. Show at least one.

## Where the rider's area comes from

The location the app sends (the addendum of 7 October 2026), turned into a pin code at once, or a pin code the rider types. No coordinates are kept. With neither, the bot asks for a pin code and sends the `request_location` action, as in v1.
