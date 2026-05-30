# asmtool — utilizare rapida

## ASM → C

```bash
python3 asm2c.py fisier.asm -o main.c
```

Extrage datele din `.data`/`.rodata`/`.bss` ca variabile globale C,
functiile ca stub-uri cu TODO-uri, pastreaza comentariile.

**Restrictii / nu merge cu:**
- salturi indirecte (`jmp rax`, `call [rax]`)
- cod fara prologue standard (`push rbp / mov rbp, rsp`)
- instructiuni SIMD (SSE/AVX)
- cod auto-modificabil sau obfuscat intentionat

---

## C → ASM (varianta buna, scrisa ca de om)

```bash
python3 c2asm_human.py main.c -o main.asm
```

Transpiler pe AST, produce NASM idiomatic cu etichete cu nume.

**Restrictii / nu merge cu:**
- `++` / `--` pe elemente de array global
- `NULL` / `stdin` fara include corespunzator
- initializare struct cu `.camp = val` (NamedInitializer)
- `sizeof` pe tipuri complexe
- `union`, `goto`, `varargs`
- array local cu initializare la declarare
- comentarii TODO nefinalizate in cod (codul trebuie sa fie C valid)

---

## C → ASM (varianta sigura, orice C)

```bash
python3 c2asm.py main.c -o main.asm
```

Foloseste gcc local + objconv. Merge cu orice C valid,
dar ASM-ul e mai mecanic (ca godbolt).

---

## Build + rulare ASM

```bash
nasm -f elf64 main.asm -o main.o && gcc -no-pie main.o -o main && ./main
```

---

## Regula practica

```
c2asm_human.py  →  daca da eroare
c2asm.py        →  fallback garantat
```
