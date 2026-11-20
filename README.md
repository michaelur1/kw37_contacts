# kw37-contacts

Přejmenování kontaktních pozic dětských hodinek **Canyon KW37 „Alfie“ (CNE-KW37)**.

Hodinky umí ukládat čísla jen do deseti pevných šablon (Táta, Máma, Dědeček, …) a žádná
aplikace k nim neexistuje. Tento program názvy šablon přepíše přímo ve firmwaru hodinek,
přes USB‑C, bez rozebírání.

*English summary: a small tool that renames the 10 fixed contact slots of the Canyon KW37
kids watch (MediaTek MT6261D) by patching the Czech UI strings in the firmware over the
boot ROM USB port. See "How it works" below.*

> **Vyloučení odpovědnosti.** Program zapisuje do paměti hodinek. Používáš ho výhradně na
> vlastní riziko. Autor neposkytuje žádnou záruku a nenese odpovědnost za poškození
> zařízení, ztrátu dat, ztrátu záruky výrobce ani jiné škody vzniklé jeho použitím.
> Před první změnou si udělej zálohu (`--dump`). Podporovaná je jen jedna verze firmwaru
> (viz níže); u jiné program skončí, aniž by cokoli zapsal.
>
> **Disclaimer.** This tool writes to the watch's flash memory. Use it entirely at your own
> risk. It is provided "as is", without warranty of any kind; the author is not liable for
> any damage to the device, loss of data or other harm resulting from its use.

## Podporované zařízení

| | |
|---|---|
| Hodinky | Canyon CNE‑KW37 (Alfie KW‑37) |
| Firmware | `JH_Y36_G12_MULTI_LANG_61D_CM032A20A2_GC9307_HC_7_9GAMES_ZY_COB_V15_20241009` |
| Čip / flash | MediaTek MT6261D, 4 MiB SPI NOR (ověřeno s GD25LQ32, JEDEC `C8 60 16`) |
| Jazyk | čeština (mění se české texty šablon) |

Verzi firmwaru najdeš v hodinkách v menu *Version Information*.

## Co budeš potřebovat

1. Windows 10/11 a datový USB‑C kabel.
2. Ovladač pro port boot ROM. Program si ho při prvním spuštění nainstaluje sám
   (samostatně to jde příkazem `kw37_contacts --install-driver`).

   Windows se zeptá na oprávnění správce. Program přiřadí portu hodinek (USB `0E8D:0003`)
   sériový ovladač `usbser.sys`, který je součástí Windows. Protože Windows vyžadují
   podepsaný instalační balíček, vytvoří se na tvém počítači jednorázový certifikát
   „kw37-contacts BROM driver (self-signed)“, zařadí se mezi důvěryhodné a jeho soukromý
   klíč se hned smaže (stejný postup používá Zadig). Vše odebereš příkazem
   `kw37_contacts --remove-driver`.

   Náhradní postup: program [Zadig](https://zadig.akeo.ie), *Device → Create New Device*,
   USB ID `0E8D` `0003`, ovladač **USB Serial (CDC)**, *Install Driver*.

   Po připojení vypnutých hodinek se pak ve Správci zařízení na pár sekund objeví
   sériový port „MediaTek BROM“. Program si ho najde sám podle USB ID.
3. `KW37-Kontakty.exe` z [Releases](../../releases), nebo Python 3.9+ a `pip install pyserial`
   (okno: `python kw37_contacts_gui.py`).

## Použití

Stačí spustit **`KW37-Kontakty.exe`** (okno) a kliknout na „Připojit hodinky“.
Totéž umí v příkazové řádce `kw37_contacts.exe` (nebo `python kw37_contacts.py`).
Program postupně:

1. zkontroluje ovladač a když chybí, sám ho nainstaluje (jednou potvrdíš oprávnění správce),
2. vyzve tě k připojení **vypnutých** hodinek a port si najde sám,
3. při prvním použití uloží vedle sebe zálohu celé paměti (`kw37_zaloha_*.bin`),
4. vypíše aktuální názvy; u každé pozice napíšeš nový název, nebo jen Enter,
5. ukáže souhrn, po napsání `ANO` zapíše změny a ověří je.

Pak odpoj kabel a hodinky zapni. Port lze zadat i ručně: `kw37_contacts.exe COM5`.

Další volby:

```
kw37_contacts --dump zaloha.bin        záloha celé flash (jen čtení) – udělej jako první!
kw37_contacts --factory                vrátí tovární názvy
kw37_contacts --set 7=Sofie --yes      bez dotazování
kw37_contacts --offline zaloha.bin --set 7=Sofie   zkouška nad zálohou, bez hodinek
```

Záloha obsahuje i osobní data (uložená čísla, IMEI). Nikam ji nenahrávej.

### Omezení délky

Nový název může mít nejvýš tolik znaků jako tovární, jinak by se posunuly ostatní texty:

| Pozice | Tovární název | Max. znaků |
|---|---|---|
| 1 | Táta | 4 |
| 2 | Máma | 4 |
| 3 | Dědeček | 7 |
| 4 | Babička | 7 |
| 5 | Bratr | 5 |
| 6 | Sestra | 6 |
| 7 | Strýc | 5 |
| 8 | Teta | 4 |
| 9 | Přítel 1 | 8 |
| 10 | Přítel 2 | 8 |

Texty se po úpravě znovu komprimují do původního místa. Výjimečně se některá kombinace
názvů nevejde; program to ohlásí a nic nezapíše.

## Když se něco pokazí

* Program visí na „Čekám na port“: hodinky nejsou vypnuté, chybí ovladač, nebo je jiný port.
* Hodinky po práci nereagují: stiskni reset v dírce na boku.
* Režim boot ROM je pevně v čipu a program na zavaděč nesahá, takže se k hodinkám dá
  připojit znovu i po nepovedeném zápisu. Před zápisem se původní obsah měněných sektorů
  ukládá do souboru `kw37_sektory_pred_zapisem_*.bin`.

## How it works

* The MT6261 boot ROM enumerates as USB `0E8D:0003` for a few seconds after plug‑in and
  offers memory read/write commands (`A2`, `D1`, `D2`, `D4`).
* Flash is read through the memory map (`0xA0510000 = 2`). Writes go through the Serial
  Flash Interface controller at `0xA0140000` in MAC mode, sending raw SPI NOR commands
  (`06`, `20`, `02`, `05`, `03`, `9F`). On this unit the reply bytes in `SFI_GPRAM` are
  only visible *after* MAC mode is released.
* UI strings are UTF‑16LE inside LZMA "alone" chunks (props `0x5C`). The Czech contact
  labels are in the chunks at `0x142D90` and `0x134290`; the chunk table entries
  (address, unpacked size, packed size) are at `0xACF68` and `0xAD208`.
* The tool decompresses a chunk, overwrites the label in place (zero padded), recompresses
  with liblzma, checks that it fits the original slot, updates the packed size in the table
  and rewrites only the changed 4 KiB sectors, verifying each one twice (SPI read and
  memory‑mapped read).
* The firmware is identified by SHA‑256 of the decompressed chunks with the label fields
  masked, so no firmware data is distributed with this project.

The contact records stored in NVRAM also contain a name, but the watch does not display it.

## Build

```
pip install pyserial pyinstaller
build.bat
```

## Credits

The boot ROM protocol and the SFI flashing sequence follow the public‑domain research in
[MTreader](https://code.luxferre.top/luxferre/mtreader) and
[MTBoss](https://code.luxferre.top/luxferre/mtboss) by Luxferre.

Not affiliated with Canyon or MediaTek.

## License

MIT © 2026 Michael Urban
