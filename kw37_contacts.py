#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michael Urban
"""kw37-contacts: rename the 10 contact slots of the Canyon KW37 "Alfie" kids watch.

The watch (MediaTek MT6261D, 4 MiB SPI NOR, firmware
JH_Y36_G12_MULTI_LANG_61D_..._V15_20241009) only offers fixed contact templates
(Táta, Máma, ...). Their Czech labels live in two LZMA chunks of the firmware.
This tool talks to the chip's boot ROM over USB, reads the chunks, replaces the
labels, recompresses them into the same slots and rewrites only the changed
4 KiB sectors, verifying every sector by read-back.

Nothing copyrighted is shipped: the firmware is recognised by SHA-256 of the
decompressed chunks (with the label fields masked out).

Usage:
    kw37_contacts                      interactive, port found automatically
    kw37_contacts COM5                 another port
    kw37_contacts --dump backup.bin    full 4 MiB flash backup (read only)
    kw37_contacts --factory            restore the factory labels
    kw37_contacts --set 7=Sofie --yes  non-interactive
    kw37_contacts --offline backup.bin --set 7=Sofie   dry run on a dump file
"""
import argparse
import datetime
import hashlib
import lzma
import os
import struct
import sys
import time

__version__ = '1.0.0'

SECTOR = 0x1000
FLASH_SIZE = 0x400000
FLASH_BASE = 0x10000000
JEDEC_CAPACITY_4MIB = 0x16

# chunk address -> (chunk table entry, end of slot, decompressed size, sha256 of masked content)
CHUNKS = {
    0x142D90: (0xACF68, 0x143BE8, 15264,
               '1b0128d2c1955f6cd39e7ea3e685b3c7a7822b8b643b390142bbb86d3e509b1d'),
    0x134290: (0xAD208, 0x1370A8, 53796,
               '12450355b06d68eb43fc1ed14d27814a2057eb27e26c7c17994ff0da16952a70'),
}
# slot on the watch -> (factory label, chunk, offset in decompressed chunk, max characters)
SLOTS = {
    1: ('Táta', 0x142D90, 0x20CA, 4), 2: ('Máma', 0x142D90, 0x20D4, 4),
    3: ('Dědeček', 0x142D90, 0x20DE, 7), 4: ('Babička', 0x134290, 0x91F4, 7),
    5: ('Bratr', 0x142D90, 0x20EE, 5), 6: ('Sestra', 0x134290, 0x920E, 6),
    7: ('Strýc', 0x142D90, 0x20FA, 5), 8: ('Teta', 0x134290, 0x921C, 4),
    9: ('Přítel 1', 0x142D90, 0x2106, 8), 10: ('Přítel 2', 0x142D90, 0x2118, 8),
}


DISCLAIMER = ('Program zapisuje do paměti hodinek. Používáš ho výhradně na vlastní riziko.\n'
              'Autor neposkytuje žádnou záruku a nenese odpovědnost za poškození zařízení,\n'
              'ztrátu dat ani jiné škody vzniklé jeho použitím.')


class Abort(Exception):
    """Stop without (further) writing; message is shown to the user."""


# ------------------------------------------------------------ BROM / flash --
class Watch:
    """MT6261 boot ROM link plus raw SPI NOR access through the SFI controller."""
    SFI = 0xA0140000
    MAC_CTL, MAC_OUTL, MAC_INL, MISC3, GPRAM = SFI, SFI + 0x10, SFI + 0x14, SFI + 0x44, SFI + 0x800
    MAC_SEL, MAC_EN, TRIG, WIP_READY, WIP = 1 << 28, 1 << 3, 1 << 2, 1 << 1, 1 << 0

    def __init__(self, port, stop=None):
        import serial
        stop = stop or (lambda: False)
        from serial.tools import list_ports
        auto = port.lower() == 'auto'
        print('Čekám na port %s ... teď připoj VYPNUTÉ hodinky.'
              % ('boot ROM (USB 0E8D:0003)' if auto else port), flush=True)
        t_wait = time.time()
        hinted = False
        while True:
            if stop():
                raise Abort('Zrušeno.')
            name = port
            if auto:
                name = next((p.device for p in list_ports.comports()
                             if p.vid == 0x0E8D and p.pid == 0x0003), None)
            try:
                if name is None:
                    raise OSError('port nenalezen')
                self.s = serial.Serial(name, 115200, rtscts=True, dsrdtr=True,
                                       timeout=0.1, write_timeout=1)
            except (OSError, serial.SerialException):
                if not hinted and time.time() - t_wait > 30:
                    hinted = True
                    print('  Port se stále neobjevil. Zkontroluj, že jsou hodinky vypnuté (reset dírkou)\n'
                          '  a že kabel umí data, ne jen nabíjení. Pak je odpoj a připoj znovu.',
                          flush=True)
                time.sleep(0.05)
                continue
            if auto:
                print(f'Nalezen port {name}.', flush=True)
            try:
                t0 = time.time()
                while True:
                    self.s.write(b'\xA0')
                    if self.s.read(1) == b'\x5F':
                        break
                    if time.time() - t0 > 5:
                        raise OSError('boot ROM neodpovídá')
                break
            except (OSError, serial.SerialException) as e:
                print(f'  ({e}) - odpoj hodinky, vypni je (reset dírkou) a připoj znovu...', flush=True)
                try:
                    self.s.close()
                except Exception:
                    pass
                time.sleep(1)
        self.s.timeout = 2
        self.s.write(b'\x0A\x50\x05')
        if self.s.read(3) != b'\xF5\xAF\xFA':
            raise Abort('Handshake s boot ROM selhal.')
        self.chip = struct.unpack('>H', self._cmd(b'\xA2' + struct.pack('>II', 0x80000008, 1), 2))[0]
        print('Připojeno, čip: %04x' % self.chip)
        if self.chip != 0x6261:
            raise Abort('Tohle není MT6261 - končím.')
        self.w16(0xA0030000, 0x2200)   # system watchdog off
        self.w16(0xA0700A28, 0x8000)   # USB download mode
        self.w16(0xA0700A24, 2)        # battery watchdog off
        self.w32(0xA0510000, 2)        # memory map mode 2: flash at address 0
        self.reg_le = None

    def _rx(self, n):
        d = self.s.read(n)
        if len(d) != n:
            raise IOError(f'krátká odpověď ({len(d)}/{n} B)')
        return d

    def _cmd(self, c, n):
        self.s.write(c)
        if self._rx(len(c)) != c:
            raise IOError('špatné echo')
        return self._rx(n) if n else b''

    def w16(self, addr, val):
        if self._cmd(b'\xD2' + struct.pack('>II', addr, 1), 2) != b'\0\1':
            raise IOError('w16 ack')
        if self._cmd(struct.pack('>H', val), 2) != b'\0\1':
            raise IOError('w16 data ack')

    def w32(self, addr, val):
        if self._cmd(b'\xD4' + struct.pack('>II', addr, 1), 2) != b'\0\1':
            raise IOError('w32 ack')
        if self._cmd(struct.pack('>I', val & 0xFFFFFFFF), 2) != b'\0\1':
            raise IOError('w32 data ack')

    def r32raw(self, addr, n):
        return self._cmd(b'\xD1' + struct.pack('>II', addr, n), 4 * n + 4)[2:-2]

    def r32(self, addr):
        return struct.unpack('<I' if self.reg_le else '>I', self.r32raw(addr, 1))[0]

    def read_mapped(self, addr, size):
        """Flash read through the memory map (same method as MTreader)."""
        out = bytearray()
        while size:
            n = min(size, 1024)
            raw = self.r32raw(addr, n // 4)
            for i in range(0, len(raw), 4):
                out += raw[i:i + 4][::-1]
            addr += n
            size -= n
        return bytes(out)

    def detect_order(self):
        t = self.GPRAM + 0x9C
        self.w32(t, 0x11223344)
        raw = self.r32raw(t, 1)
        if raw == b'\x44\x33\x22\x11':
            self.reg_le = True
        elif raw == b'\x11\x22\x33\x44':
            self.reg_le = False
        else:
            raise Abort(f'Samotest registrů selhal ({raw.hex()}).')

    def mac(self, out, inlen=0):
        """One raw SPI transaction. Reply bytes become visible in GPRAM only after MAC release."""
        assert len(out) + inlen <= 160
        buf = out + b'\xFF' * inlen if 0 < inlen <= 4 else out
        for i in range(0, len(buf), 4):
            self.w32(self.GPRAM + i, int.from_bytes(buf[i:i + 4].ljust(4, b'\xFF' if inlen else b'\0'), 'little'))
        misc3 = self.r32(self.MISC3)
        self.w32(self.MISC3, misc3 | (1 << 9))
        mv = self.MAC_SEL | self.MAC_EN
        self.w32(self.MAC_CTL, mv)
        self.w32(self.MAC_OUTL, len(out))
        self.w32(self.MAC_INL, inlen)
        self.w32(self.MAC_CTL, mv | self.TRIG)
        t0 = time.time()
        while True:
            v = self.r32(self.MAC_CTL)
            if (v & self.WIP_READY) and not (v & self.WIP):
                break
            if time.time() - t0 > 2:
                self.w32(self.MAC_CTL, 0)
                self.w32(self.MISC3, misc3 & ~(1 << 9))
                raise IOError('SFI MAC operace nedoběhla (MAC_CTL=%08x)' % v)
        self.w32(self.MAC_CTL, 0)
        self.w32(self.MISC3, misc3 & ~(1 << 9))
        res = b''
        if inlen:
            tot = len(out) + inlen
            for i in range(0, tot, 4):
                res += struct.pack('<I', self.r32(self.GPRAM + i))
            res = res[len(out):tot]
        return res

    def jedec(self):
        return self.mac(b'\x9F', 3)

    def sr(self):
        return self.mac(b'\x05', 1)[0]

    def wait_ready(self, timeout=5):
        t0 = time.time()
        while self.sr() & 1:
            if time.time() - t0 > timeout:
                raise IOError('flash zůstává zaneprázdněná')
            time.sleep(0.005)

    def read_spi(self, addr, size):
        out = b''
        while size:
            n = min(size, 128)
            out += self.mac(bytes([0x03]) + addr.to_bytes(3, 'big'), n)
            addr += n
            size -= n
        return out

    def wren(self):
        self.mac(b'\x06')
        if not self.sr() & 0x02:
            raise IOError('Write Enable se nenastavil')

    def erase4k(self, addr):
        self.wait_ready()
        self.wren()
        self.mac(b'\x20' + addr.to_bytes(3, 'big'))
        self.wait_ready(10)

    def program(self, addr, data):
        for o in range(0, len(data), 64):
            chunk = data[o:o + 64]
            if chunk == b'\xFF' * len(chunk):
                continue
            self.wait_ready()
            self.wren()
            self.mac(b'\x02' + (addr + o).to_bytes(3, 'big') + chunk)
            self.wait_ready()

    def write_sector(self, addr, data):
        """Erase + program + verify one 4 KiB sector (two attempts)."""
        for attempt in (1, 2):
            print(f'  0x{addr:06X}: mažu...', end='', flush=True)
            self.erase4k(addr)
            print(' zapisuji...', end='', flush=True)
            self.program(addr, data)
            if self.read_spi(addr, SECTOR) == data and self.read_mapped(addr, SECTOR) == data:
                print(' ověřeno OK')
                return True
            print(f' OVĚŘENÍ SELHALO (pokus {attempt})')
        return False

    def selftest(self):
        self.detect_order()
        jid = self.jedec()
        print('Flash JEDEC ID: %s, stavový registr: 0x%02X' % (jid.hex(' '), self.sr()))
        if jid[0] in (0x00, 0xFF) or jid[2] != JEDEC_CAPACITY_4MIB:
            raise Abort('Flash neodpovídá jako 4 MiB SPI NOR - končím, nic nezapisuji.')
        if self.sr() & 0x3C:
            raise Abort('Flash má zapnutou ochranu proti zápisu - končím, nic nezapisuji.')
        head = self.read_mapped(0, 256)      # read_mapped works in whole 4-byte words
        if self.read_spi(0, 256) != head or head[:7] != b'SF_BOOT':
            raise Abort('Samotest čtení selhal - končím, nic nezapisuji.')

    def reset(self):
        try:
            self.w16(0xA003001C, 0x1209)
        except Exception:
            pass


# ---------------------------------------------------------------- patching --
def sectors_of(a, b):
    return list(range(a & ~(SECTOR - 1), b, SECTOR))


def all_sectors():
    s = set()
    for c, (tab, end, _, _) in CHUNKS.items():
        s.update(sectors_of(c, end))
        s.update(sectors_of(tab, tab + 12))
    return sorted(s)


def rd(img, a, n):
    out = b''
    while n:
        s = a & ~(SECTOR - 1)
        o = a - s
        k = min(n, SECTOR - o)
        out += bytes(img[s][o:o + k])
        a += k
        n -= k
    return out


def wr(img, a, data):
    i = 0
    while i < len(data):
        s = a & ~(SECTOR - 1)
        o = a - s
        k = min(len(data) - i, SECTOR - o)
        img[s][o:o + k] = data[i:i + k]
        a += k
        i += k


def field(n):
    return (SLOTS[n][3] + 1) * 2          # bytes including the terminator


def mask_labels(data, c):
    d = bytearray(data)
    for n, (_, ch, off, _) in SLOTS.items():
        if ch == c:
            d[off:off + field(n)] = bytes(field(n))
    return bytes(d)


def unpack_chunk(img, c):
    tab, end, usize, sha = CHUNKS[c]
    taddr, tus, csize = struct.unpack('<III', rd(img, tab, 12))
    blob = rd(img, c, end - c)
    if (taddr != FLASH_BASE + c or tus != usize or not 13 < csize <= end - c
            or blob[0] != 0x5C or struct.unpack('<Q', blob[5:13])[0] != usize):
        raise Abort('Tento firmware neznám (tabulka bloků nesedí) - končím, nic nezapisuji.')
    try:
        d = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        data = d.decompress(blob[:5] + b'\xff' * 8 + blob[13:csize], max_length=usize)
    except lzma.LZMAError:
        data = b''
    if len(data) != usize or hashlib.sha256(mask_labels(data, c)).hexdigest() != sha:
        raise Abort('Tento firmware neznám (blok textů se liší) - končím, nic nezapisuji.')
    return bytearray(data), blob[:13]


def compress_best(data):
    best = None
    for ds in (1 << 14, 1 << 16, 1 << 20):
        for mf in (lzma.MF_BT4, lzma.MF_BT2, lzma.MF_HC4):
            for nice in (64, 128, 273):
                f = [{'id': lzma.FILTER_LZMA1, 'lc': 2, 'lp': 0, 'pb': 2, 'dict_size': ds,
                      'mf': mf, 'nice_len': nice, 'mode': lzma.MODE_NORMAL, 'depth': 0}]
                c = lzma.compress(data, format=lzma.FORMAT_ALONE, filters=f)
                if best is None or len(c) < len(best):
                    best = c
    return best


def load_state(img):
    chunks = {c: unpack_chunk(img, c) for c in CHUNKS}
    labels = {}
    for n, (_, c, off, _) in SLOTS.items():
        raw = bytes(chunks[c][0][off:off + field(n)])
        labels[n] = raw.decode('utf-16-le', 'replace').split('\0')[0]
    return chunks, labels


def check_label(n, label):
    mx = SLOTS[n][3]
    if not label or len(label) > mx:
        raise Abort(f'Pozice {n}: název {label!r} musí mít 1 až {mx} znaků.')
    if '\0' in label or len(label.encode('utf-16-le')) != 2 * len(label):
        raise Abort(f'Pozice {n}: název {label!r} obsahuje nepodporovaný znak.')


def build(img, chunks, new_labels):
    new = {s: bytearray(d) for s, d in img.items()}
    for c, (data, hdr) in chunks.items():
        data = bytearray(data)
        mine = {n: v for n, v in new_labels.items() if SLOTS[n][1] == c}
        if not mine:
            continue
        for n, label in mine.items():
            check_label(n, label)
            off = SLOTS[n][2]
            nb = label.encode('utf-16-le')
            data[off:off + field(n)] = nb + bytes(field(n) - len(nb))
        tab, end, _, _ = CHUNKS[c]
        comp = compress_best(bytes(data))
        blob = hdr + comp[13:]
        if len(blob) > end - c:
            raise Abort(f'Tato kombinace názvů se po zabalení nevejde ({len(blob)} B, místo {end - c} B). '
                        'Zkus kratší nebo jiné názvy. Nic se nezapsalo.')
        d2 = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        if d2.decompress(hdr[:5] + b'\xff' * 8 + comp[13:]) != bytes(data) or not d2.eof:
            raise Abort('Kontrolní rozbalení nesedí - končím, nic nezapisuji.')
        wr(new, c, blob + bytes(end - c - len(blob)))
        wr(new, tab + 8, struct.pack('<I', len(blob)))
    return new


# ---------------------------------------------------------- driver install --
# The boot ROM port (USB 0E8D:0003, class 0x0A) has no in-box driver binding, so
# Windows needs an INF that points it at its own usbser.sys. An INF must be
# covered by a signed catalog. Like Zadig/libwdi we create a one-time self-signed
# certificate, sign the catalog, trust the certificate on this PC and immediately
# delete its private key, so nothing else can ever be signed with it.
DRIVER_SUBJECT = 'CN=kw37-contacts BROM driver (self-signed)'

DRIVER_INF = r'''; kw37-contacts: binds the MediaTek boot ROM port to the in-box usbser.sys
[Version]
Signature   = "$Windows NT$"
Class       = Ports
ClassGuid   = {4D36E978-E325-11CE-BFC1-08002BE10318}
Provider    = %Provider%
CatalogFile = kw37_brom.cat
DriverVer   = 11/21/2026,1.0.1.0
PnpLockdown = 1

[Manufacturer]
%Provider% = Devices,NTamd64,NTarm64,NTx86

[Devices.NTamd64]
%DeviceName% = Brom_Install,USB\VID_0E8D&PID_0003

[Devices.NTarm64]
%DeviceName% = Brom_Install,USB\VID_0E8D&PID_0003

[Devices.NTx86]
%DeviceName% = Brom_Install,USB\VID_0E8D&PID_0003

[Brom_Install.NT]
Include = usbser.inf
Needs   = UsbSerial_Install.NT

[Brom_Install.NT.Hw]
Include = usbser.inf
Needs   = UsbSerial_Install.NT.Hw

[Brom_Install.NT.Services]
Include = usbser.inf
Needs   = UsbSerial_Install.NT.Services

[Strings]
Provider   = "kw37-contacts"
DeviceName = "MediaTek BROM (kw37-contacts)"
'''

DRIVER_PS_INSTALL = r'''param([string]$Dir, [string]$Subject)
$ErrorActionPreference = 'Stop'
$log = Join-Path $Dir 'install.log'
function Log($m) { Add-Content -Path $log -Value $m -Encoding UTF8 }
try {
    $inf = Join-Path $Dir 'kw37_brom.inf'; $cat = Join-Path $Dir 'kw37_brom.cat'; $cer = Join-Path $Dir 'kw37_brom.cer'
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
        -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(10) -KeyExportPolicy NonExportable
    New-FileCatalog -Path $inf -CatalogFilePath $cat -CatalogVersion 2.0 | Out-Null
    $sig = Set-AuthenticodeSignature -FilePath $cat -Certificate $cert -HashAlgorithm SHA256
    Log "SIGN=$($sig.Status)"
    Export-Certificate -Cert $cert -FilePath $cer | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
    Remove-Item -Path "Cert:\LocalMachine\My\$($cert.Thumbprint)" -DeleteKey
    Log "CERT=$($cert.Thumbprint)"
    $out = & pnputil.exe /add-driver $inf /install 2>&1 | Out-String
    Log $out
    Log "EXIT=$LASTEXITCODE"
} catch {
    Log "ERROR=$($_.Exception.Message)"
}
'''

DRIVER_PS_REMOVE = r'''param([string]$Dir, [string]$Subject)
$ErrorActionPreference = 'Continue'
$log = Join-Path $Dir 'install.log'
function Log($m) { Add-Content -Path $log -Value $m -Encoding UTF8 }
try {
    $lines = & pnputil.exe /enum-drivers
    for ($i = 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match 'kw37_brom\.inf' -and $lines[$i - 1] -match '(oem\d+\.inf)') {
            Log (& pnputil.exe /delete-driver $Matches[1] /uninstall /force 2>&1 | Out-String)
        }
    }
    foreach ($store in 'Root', 'TrustedPublisher', 'My') {
        Get-ChildItem "Cert:\LocalMachine\$store" | Where-Object { $_.Subject -eq $Subject } |
            ForEach-Object { Remove-Item $_.PSPath -ErrorAction SilentlyContinue; Log "REMOVED=$store $($_.Thumbprint)" }
    }
    Log 'EXIT=0'
} catch {
    Log "ERROR=$($_.Exception.Message)"
}
'''


def driver_action(install=True):
    """Install/remove the boot ROM port driver via an elevated PowerShell (UAC prompt)."""
    import subprocess
    import tempfile
    if os.name != 'nt':
        raise Abort('Instalace ovladače je jen pro Windows (na Linuxu a macOS není potřeba).')
    # Not mkdtemp(): its owner-only ACL makes files written by the elevated process
    # (owned by Administrators) unreadable for us afterwards.
    d = os.path.join(tempfile.gettempdir(), 'kw37_driver_%d_%d' % (os.getpid(), int(time.time())))
    os.makedirs(d)
    with open(os.path.join(d, 'kw37_brom.inf'), 'w', encoding='ascii', newline='\r\n') as f:
        f.write(DRIVER_INF)
    ps1 = os.path.join(d, 'driver.ps1')
    with open(ps1, 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write(DRIVER_PS_INSTALL if install else DRIVER_PS_REMOVE)
    if install:
        print('Nainstaluji ovladač portu hodinek (USB 0E8D:0003 -> sériový port Windows).\n'
              'Na tomto počítači se k tomu vytvoří jednorázový certifikát\n'
              f'  „{DRIVER_SUBJECT[3:]}“,\n'
              'přidá se mezi důvěryhodné a jeho soukromý klíč se hned smaže.\n'
              'Odebrat vše lze později volbou --remove-driver.')
    print('Windows se teď zeptá na oprávnění správce (UAC) - potvrď ho.')
    inner = ("Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
             "'-NoProfile','-ExecutionPolicy','Bypass','-File','\"%s\"','-Dir','\"%s\"','-Subject','\"%s\"'"
             % (ps1, d, DRIVER_SUBJECT))
    r = subprocess.run(['powershell.exe', '-NoProfile', '-Command', inner], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, creationflags=0x08000000)   # CREATE_NO_WINDOW
    logp = os.path.join(d, 'install.log')
    log = open(logp, encoding='utf-8-sig', errors='replace').read() if os.path.exists(logp) else ''
    if not log:
        raise Abort('Operace neproběhla (oprávnění správce nebylo potvrzeno?).\n' + r.stderr.strip())
    ok = 'EXIT=0' in log or 'EXIT=3010' in log or 'EXIT=259' in log
    if not ok or 'ERROR=' in log:
        raise Abort('Operace s ovladačem selhala. Záznam:\n' + log.strip() +
                    '\nNáhradní postup: nainstaluj ovladač programem Zadig (viz README).')
    print('Ovladač je nainstalovaný. Teď můžeš připojit vypnuté hodinky.' if install
          else 'Ovladač i certifikát jsou odebrané.')


def driver_ready():
    """True if Windows already knows a serial driver for USB 0E8D:0003 (ours or Zadig's)."""
    if os.name != 'nt':
        return True
    # Look for an installed driver package naming this device. (The device's registry
    # node is not a reliable sign: it survives removal of the driver package.)
    import glob
    needle = 'VID_0E8D&PID_0003'
    for path in glob.glob(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'INF', 'oem*.inf')):
        try:
            with open(path, 'rb') as f:
                raw = f.read().upper()
        except OSError:
            continue
        if needle.encode('ascii') in raw or needle.encode('utf-16-le') in raw:
            return True
    return False


def ensure_driver(ask=True):
    if driver_ready():
        return
    print('\nKROK 1: ovladač\nNa tomto počítači ještě není ovladač pro port hodinek.')
    if ask:
        input('Stiskni Enter, nainstaluji ho (Windows požádá o oprávnění správce)...')
    driver_action(install=True)


def app_dir():
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))


def read_sectors(dev):
    """Read all sectors we may touch, by two independent paths."""
    img = {}
    for s in all_sectors():
        m = dev.read_mapped(s, SECTOR)
        if m != dev.read_spi(s, SECTOR):
            raise Abort(f'Sektor 0x{s:06X}: dvě cesty čtení se liší - končím.')
        img[s] = bytearray(m)
    return img


def backup_if_first_use(dev):
    import glob
    if not glob.glob(os.path.join(app_dir(), 'kw37_zaloha_*.bin')):
        print('\nZÁLOHA: první použití, ukládám zálohu celé paměti hodinek (asi 15 s)...')
        do_dump(dev, os.path.join(app_dir(), 'kw37_zaloha_%s.bin'
                                  % datetime.datetime.now().strftime('%Y%m%d_%H%M%S')))


def write_changes(dev, img, new):
    """Write changed sectors with verification; restores the previous content on failure."""
    changed = [s for s in all_sectors() if new[s] != img[s]]
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    bak = os.path.join(app_dir(), f'kw37_sektory_pred_zapisem_{stamp}.bin')
    with open(bak, 'wb') as f:
        for s in changed:
            f.write(struct.pack('<I', s) + bytes(img[s]))
    print(f'Původní obsah měněných sektorů uložen do {bak}.')
    for s in changed:
        if not dev.write_sector(s, bytes(new[s])):
            print(f'!!! Zápis 0x{s:06X} selhal, vracím předchozí obsah...')
            ok = all(dev.write_sector(r, bytes(img[r])) for r in changed)
            raise Abort('Zápis se nepovedl, předchozí stav je vrácen.' if ok else
                        'Vrácení selhalo. Hodinky nezapínej a spusť program znovu.')


# --------------------------------------------------------------------- CLI --
def do_dump(dev, path):
    if os.path.exists(path):
        raise Abort(f'Soubor {path} už existuje, nepřepisuji ho.')
    data = bytearray()
    while len(data) < FLASH_SIZE:
        data += dev.read_mapped(len(data), 0x10000)
        print(f'\r  přečteno {len(data) * 100 // FLASH_SIZE} %', end='', flush=True)
    with open(path, 'wb') as f:
        f.write(data)
    print(f'\nUloženo: {path}\nSHA256: {hashlib.sha256(data).hexdigest()}')
    print('Pozor: záloha obsahuje i osobní data (kontakty, IMEI). Nezveřejňuj ji.')


def run(a):
    secs = all_sectors()
    dev = None
    try:
        if a.offline:
            with open(a.offline, 'rb') as f:
                full = f.read()
            if len(full) != FLASH_SIZE:
                raise Abort(f'{a.offline} není 4 MiB záloha flash.')
            img = {s: bytearray(full[s:s + SECTOR]) for s in secs}
        else:
            ensure_driver(ask=not a.yes)
            print('\nPŘIPOJENÍ: hodinky vypni. Připoj je kabelem, až se objeví „Čekám na port“.')
            dev = Watch(a.port)
            dev.selftest()
            if a.dump:
                do_dump(dev, a.dump)
                return
            if not a.no_backup:
                backup_if_first_use(dev)
            img = read_sectors(dev)

        chunks, labels = load_state(img)
        print('\nAktuální názvy v hodinkách:')
        for n in SLOTS:
            print(f'  {n:2d}. {labels[n]:10s} (tovární: {SLOTS[n][0]}, max {SLOTS[n][3]} znaků)')

        new_labels = {n: SLOTS[n][0] for n in SLOTS} if a.factory else {}
        for s in a.set:
            k, _, v = s.partition('=')
            if not k.isdigit() or int(k) not in SLOTS:
                raise Abort(f'Neplatná pozice v --set {s!r} (1 až 10).')
            new_labels[int(k)] = v
        if not a.set and not a.factory:
            print('\nZadej nové názvy. Enter = nechat beze změny.')
            for n in SLOTS:
                while True:
                    v = input(f'  {n:2d}. {labels[n]} -> ').strip()
                    if v and len(v) > SLOTS[n][3]:
                        print(f'      Příliš dlouhé, maximum je {SLOTS[n][3]} znaků.')
                        continue
                    break
                if v:
                    new_labels[n] = v
        new_labels = {n: v for n, v in new_labels.items() if v != labels[n]}
        if not new_labels:
            print('Žádná změna.')
            return
        new = build(img, chunks, new_labels)
        changed = [s for s in secs if new[s] != img[s]]
        print('\nZměny:')
        for n, v in sorted(new_labels.items()):
            print(f'  {n:2d}. {labels[n]} -> {v}')
        print('Sektory k přepsání:', ', '.join('0x%06X' % s for s in changed))

        if dev is None:
            if a.out:
                full = bytearray(full)
                for s in changed:
                    full[s:s + SECTOR] = new[s]
                with open(a.out, 'wb') as f:
                    f.write(full)
                print(f'Výsledný obraz uložen do {a.out}.')
            print('Simulace OK (do hodinek se nic nezapsalo).')
            return

        if not a.yes and input('\nZápis provádíš na vlastní riziko. Napiš ANO pro zápis do hodinek: ').strip() != 'ANO':
            print('Zrušeno, nic se nezapsalo.')
            return
        write_changes(dev, img, new)
        print('\nHOTOVO. Odpoj kabel a zapni hodinky.')
    finally:
        if dev is not None:
            dev.reset()


def main():
    ap = argparse.ArgumentParser(prog='kw37_contacts',
                                 description='Přejmenování 10 kontaktních pozic hodinek Canyon KW37.')
    ap.add_argument('port', nargs='?', default='auto', help='sériový port boot ROM (výchozí: najít automaticky)')
    ap.add_argument('--set', action='append', default=[], metavar='N=NÁZEV', help='název pozice N (1-10)')
    ap.add_argument('--factory', action='store_true', help='vrátit tovární názvy')
    ap.add_argument('--yes', action='store_true', help='neptat se na potvrzení zápisu')
    ap.add_argument('--dump', metavar='SOUBOR', help='jen uložit zálohu celé flash (4 MiB)')
    ap.add_argument('--offline', metavar='DUMP', help='simulace nad souborem se zálohou')
    ap.add_argument('--out', metavar='SOUBOR', help='s --offline: uložit výsledný obraz')
    ap.add_argument('--no-backup', action='store_true', help='nedělat automatickou zálohu při prvním použití')
    ap.add_argument('--install-driver', action='store_true', help='jen nainstalovat ovladač portu (chce správce)')
    ap.add_argument('--remove-driver', action='store_true', help='odebrat ovladač a jeho certifikát')
    ap.add_argument('--version', action='version', version=__version__)
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    print(f'kw37-contacts {__version__} | (c) 2026 Michael Urban | licence MIT\n{DISCLAIMER}\n')
    code = 0
    try:
        if a.install_driver or a.remove_driver:
            driver_action(install=a.install_driver)
        else:
            run(a)
    except Abort as e:
        print(f'\n{e}')
        code = 1
    except (IOError, OSError) as e:
        print(f'\nChyba komunikace: {e}\nOdpoj hodinky, vypni je (reset dírkou) a zkus to znovu.')
        code = 1
    except KeyboardInterrupt:
        print('\nPřerušeno.')
        code = 1
    interactive = not (a.offline or a.set or a.factory or a.yes or a.dump or a.remove_driver)
    if interactive and sys.stdin and sys.stdin.isatty():
        try:
            input('\nEnter pro zavření...')
        except EOFError:
            pass
    return code


if __name__ == '__main__':
    sys.exit(main())
