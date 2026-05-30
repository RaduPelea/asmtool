# PCLP2 asmtool — rezolvă subiectele de asm în C, build pe x86_64 (64 biți)

Setul ăsta de tooluri rezolvă 3 probleme deodată:

1. **Vrei să gândești în C, nu în asm.** `asm2c` îți face dintr-un `file.asm`
   un `main.c` simplu (date + TODO-uri), îl completezi în C, apoi `c2asm`
   îți dă înapoi un `.asm` NASM valid.
2. **Template-urile sunt pe 32 de biți, tu vrei 64.** Tot ce produce tool-ul
   e `elf64` / NASM 64-bit. Pentru cine scrie direct în asm, `port64`
   convertește scheletul 32-bit în 64-bit.
3. **Mașina ta e arm64, dar îți trebuie x86_64.** Tot ce ține de build/run se
   întâmplă într-un container Docker `--platform=linux/amd64`, deci e x86_64
   real (nasm + gcc + objconv), indiferent că hostul e Apple Silicon.

## Setup (o singură dată)

```bash
cd asmtool
docker build --platform=linux/amd64 -t pclp2-x86 .   # ~câteva minute sub emulare
python3 -m pip install --user pycparser               # pentru c2asm_human.py
```
(Toolurile construiesc imaginea Docker automat dacă lipsește.)

## Workflow principal: asm → C → asm

```bash
# 1. din template-ul de subiect (32 sau 64 biți) -> main.c simplu
./asm2c.py file.asm -o main.c

# 2. completezi main.c în C, normal (for/while, printf, strstr, &, |, <<, ...)

# 3a. RECOMANDAT: NASM idiomatic, scris ca de om (transpiler propriu)
./c2asm_human.py main.c -o main.asm --run

# 3b. ALTERNATIV: NASM corect dar mecanic (gcc + objconv), pentru orice C
./c2asm.py main.c -o main.asm --run
```

### Două căi înapoi spre asm

| | `c2asm_human.py` | `c2asm.py` |
|---|---|---|
| cum | transpiler pe AST (pycparser) | gcc x86_64 + objconv |
| stil | **scris ca de om**: SysV, etichete cu nume, valori în registre callee-saved, `%define`, comentarii | output de compilator (ca godbolt) |
| acoperire | subsetul de C de la examen | **orice** C valid |
| la C nesuportat | dă eroare clară (nu cod greșit) | merge mereu |

`c2asm_human` e răspunsul la „nu vreau output de godbolt". Acceptă subsetul de C
folosit la examen:

- control: `for` / `while` / `do-while` / `if`-`else` / `switch`-`case`-`default`,
  `break`, `continue`, `?:`, `&& || !`;
- date: globale/secțiuni, vectori (inclusiv **matrice 2D** `m[i][j]`), constante
  **caracter** (`'A'`, `'\n'`, `c - '0'`), string-uri;
- aritmetică + operatori pe biți (`& | ^ << >> ~`), `%`, `/`, semne (signed/unsigned),
  `__builtin_popcount` → `popcnt`;
- pointeri: dereferențiere `*p` (citire/scriere), **aritmetică de pointeri**
  scalată (`p++`, `p + i`, `*(p+i)`), `&x`;
- **pointeri la funcții ca parametru** (`void (*f)(int*)`, `int (*g)(int,int)`):
  apel indirect `call reg`, pasarea unei funcții ca argument;
- apeluri stdlib (`printf`, `strstr`, `strlen`, `strcpy`, `malloc`, `free`, `exit`…)
  și funcții proprii, cu argumente oricât de imbricate (marshaling aliniat);
- **structuri** (`struct`/`typedef`, `.` și `->`, `s.arr[i]`, `&s`,
  `sizeof(struct)`, parametri pointer-la-struct).

Etichetele de date primesc `:` (un nume ca `str`, care e și mnemonic — `STR` —
e parsat corect ca label). `asm2c` traduce și `struc ... endstruc` /
`istruc ... iend` din NASM în `struct` C și unește liniile continuate cu `\`
(matrice pe mai multe rânduri).

Pentru structuri, asm-ul generat **emite definiția `struc ... endstruc`**,
inițializează instanțele cu **`istruc ... at ... iend`** și folosește offset-uri
**simbolice** — `[bf + binary_fun_t.maskedn]` și `binary_fun_t_size`, nu numere
hardcodate — exact ca în template-ul de subiect.
Dacă întâlnește ceva în afara subsetului, **oprește cu un mesaj** în loc să
genereze cod greșit — și folosești `c2asm.py` ca plasă de siguranță.

**Aliniere de stivă (ABI).** `c2asm_human` respectă System V: RSP e aliniat la
16 octeți la fiecare `call`. Concret — prologul adaugă 8 octeți de padding când
numărul de registre callee-saved salvate e impar; epilogul dealocă localele
înainte de `pop`-uri (deci registrele callee-saved chiar se restaurează corect);
iar salvările temporare care înconjoară un apel imbricat folosesc o rezervare
aliniată la 16, nu un `push` simplu. Verificat cu un probe ABI (vezi
`demo_align/`) pe apeluri în bucle, cu locale și apeluri imbricate în expresii.

Ambele variante **verifică automat** rezultatul (asamblare `nasm -f elf64` +
link + rulare) cu `--run`. Categoriile cerute de subiecte sunt acoperite:

| categorie subiect            | acoperit prin                          |
|------------------------------|----------------------------------------|
| apel funcții stdlib          | `call printf` / `strstr` + `extern`    |
| operații pe biți, operatori  | `and/or/xor/shl/shr/test`              |
| paritate număr de biți       | `__builtin_popcount(x) % 2`            |
| aritmetică                   | `add/sub/imul/idiv/...`                |
| afișare                      | `printf` (sau PRINTF64 dacă scrii asm) |
| variabile / secțiuni         | `.data` / `.rodata` / `.bss`           |

## Tooluri

| tool            | ce face |
|-----------------|---------|
| `asm2c.py`      | `file.asm` → `main.c` curat (date din `.data/.rodata/.bss` → globale C, semnături `void f(...)` → stub-uri, `; TODO x:` → comentarii). Nu arată ca Ghidra. |
| `c2asm_human.py`| `main.c` → `main.asm` NASM 64-bit **idiomatic** (transpiler pe AST). Necesită `pip install pycparser`. Acoperă subsetul de examen. |
| `c2asm.py`      | `main.c` → `main.asm` NASM 64-bit via gcc+objconv (orice C, dar output mecanic). |
| `port64.py`     | `file.asm` 32-bit → `main.asm` 64-bit (PRINTF32→PRINTF64, ebp→rbp etc.). Pentru cine scrie direct în asm. |
| `asmrun.sh`     | asamblează (`-f elf64`), linkează (`-no-pie`) și rulează orice `.asm` în Docker x86_64. `--gdb` pentru debug. |
| `templates/`    | `printf64.asm` + `Makefile` pe `elf64` (varianta 64-bit a template-urilor). |

### Exemple

```bash
./asm2c.py file.asm -o main.c            # extrage date + TODO-uri în C
./c2asm_human.py main.c -o main.asm --run  # C -> NASM idiomatic + rulează
./c2asm.py main.c --run                  # C -> NASM (gcc+objconv), orice C
./port64.py file.asm -o main.asm         # port direct 32->64 (fără C)
./asmrun.sh main.asm                     # build + run un .asm pe x86_64
./asmrun.sh main.asm --gdb               # debug în gdb
```

## Note

- Build-urile rulează **sub `/Users`**: Docker Desktop pe macOS nu partajează
  `/tmp` sau `/var/folders`, deci ține fișierele în arborele proiectului.
- `c2asm` folosește `-no-pie` (ca template-ul), `-O0` implicit pentru cod
  citibil. Ridică la `-O1/-O2` dacă vrei asm mai compact.
- NASM-ul produs de objconv e valid (`section .text/.data/.bss/.rodata`,
  `extern printf`, `default rel`) și se asamblează cu `nasm -f elf64`.
```
```
