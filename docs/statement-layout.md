# Bank statement PDF layout

Derived from `tools/anonymize_structure.py` output of a real statement (pages 1–2 of 11).
No real data is recorded here. Words shown in quotes were visible in the anonymized
output; words marked *(inferred)* were masked and are guessed from their length.

## Page structure

- A4 (595 × 842 pt), text-based PDF (no OCR needed), no tables detected by pdfplumber,
  so parsing is position-based (word x-coordinates).
- Page header (every page): "Tiliote" + page number "N (total)", period "d.m. - d.m.yyyy",
  "Tilinumero" + IBAN, "BIC" + code. Page 1 also has the account holder's name and address.
- Column header (3 lines, font size 8), ends with the line starting with "Arvopäivä".
- Page footer: a single word at x≈536 (y≈738), probably "Jatkuu" *(inferred)*, then
  bank contact details in font size 7 (y ≥ 750).
- **Transactions continue across pages**: page 2 begins with the "Viesti:" block of the
  last transaction on page 1.

## Columns (x of left edge)

| x | Header | Content |
|---|---|---|
| ≈58 | Kirjauspäivä *(inferred)* / "Arvopäivä" | Date `d.m.yyyy`, no zero padding |
| ≈142–160 | "Määrä, euroa" | Amount, right-aligned |
| ≈190 | "Selitys" / Maksunsaaja/maksaja *(inferred)* / "Viesti" | Type, counterparty, message |
| ≈343 | "Viite" | Reference number (card payments) |
| ≈469 | "Arkistointitunnus" / Saajan tilinumero / Saajan pankin BIC *(inferred)* | Archive ID, counterparty IBAN, BIC |
| ≈572 | – | Single-letter flag after archive ID (meaning unknown) |

## Amounts

- Negative sign is the Unicode minus `−` (U+2212), not `-`. Positive amounts have `+`.
- Decimal comma, and the thousands separator is a space, which splits the number into
  two words: `+9 999,99` → words `+9` and `999,99`.

## Balances and totals

Line before the first transaction: `+9 999,99  Saldo  99.9.9999` (amount, the word
"Saldo", date of the day before the period). After the last transaction:

```
  9 999,99  Panot yhteensä 9 kpl     ← deposits total (no sign) and count
 −9 999,99  Otot yhteensä 99 kpl     ← withdrawals total and count
 +9 999,99  Saldo 99.9.9999          ← closing balance
```

The parser validates against all of these (`Statement.validation_errors`).

## Transaction blocks

Line 1 always has: date (x≈58), amount, type (x≈190), optional reference (x≈343),
archive ID (x≈469), flag letter (x≈572). Following lines until the next date line:

**Transfer ("TILISIIRTO"), e.g. MobilePay:**
```
9.9.9999  −99,99  TILISIIRTO                99999999999999999999  X
                  MobilePay                 XX99 9999 9999 9999 99     ← counterparty, IBAN
                  Viesti:                   XXXXXXXXXXX                ← BIC
                  xx-99999999999                                       ← message
```

**Vipps MobilePay transfer (since the MobilePay → Vipps rebrand)**, reported by the user;
the message marker is uppercase, without a colon, on the same line as the name:
```
9.9.9999  +99,99  TILISIIRTO                99999999999999999999  X
                  VIPPS MOBILEPAY AS, VIESTI SEPA PIKASIIRTO
                  Maksajan viite: pp-9999999
                  99999999999999999999999
```
The parser accepts "Viesti"/"VIESTI", with or without a colon, at the start of a line or
inline. Exact line breaks and IBAN/BIC placement for this variant are not yet confirmed
with an anonymized dump.

**Card payment (12-letter uppercase type, probably PKORTTIMAKSU *(inferred)*):**
```
9.9.9999  −99,99  XXXXXXXXXXXX  999999      999999999XXXX9999999  X
                  Xxxxxxxx Xxxxxxxx X Xxxxx                            ← merchant
                  Viesti:
                  999999******9999 XXXXXXX 999999                      ← masked card no., purchase date?
                  XX NRO 99999999999999999999999
                  XXXXXXXXXX 999
```

## Still unknown

- Incoming transactions (salary, refunds): `+` amount and payer layout are assumed to
  match outgoing ones; the count/total validation will reveal it if not.
- Transaction types other than TILISIIRTO and the card payment type.
- Whether "Arvopäivä" is ever printed as a separate value-date line.
