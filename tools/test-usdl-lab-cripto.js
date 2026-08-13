/**
 * Valida la criptografia embebida en `usdl-lab.html`.
 *
 *     node tools/test-usdl-lab-cripto.js
 *
 * Esa herramienta firma transacciones sin MetaMask, asi que lleva keccak-256,
 * secp256k1 y RLP escritos a mano. Un error ahi no da un mensaje claro: la red
 * rechaza la transaccion, o peor, la acepta atribuyendola a otra cuenta.
 *
 * Los valores esperados salen de dos fuentes independientes: los vectores
 * publicados (keccak, EIP-155, EIP-55) y la implementacion en C de `eth_utils`.
 *
 * La prueba mas fuerte es la ultima: escribe `muestras_firmadas.json` para que
 * `eth_account` (Python) recupere el firmante de cada transaccion. Si la firma
 * estuviera mal, recuperaria otra direccion.
 *
 *     python -c "import json;from eth_account import Account; \
 *       [print(Account.recover_transaction(m['raw']) == m['esperado'], m['nombre']) \
 *        for m in json.load(open('tools/muestras_firmadas.json'))]"
 */
const fs = require("fs");
const path = require("path");

const HTML = fs.readFileSync(path.join(__dirname, "usdl-lab.html"), "utf8");
const script = HTML.match(/<script>([\s\S]*?)<\/script>/)[1];
// El corte deja abierto el comentario del separador: hay que cerrarlo.
const cripto = script.split("FIN CRIPTO")[0] + " */";

const C = new Function("crypto", "TextEncoder", cripto + `
  return {keccak256, claveADireccion, conChecksum, firmarTx, firmar, rlp,
          hexABytes, bytesAHex, bigIntABytes, pMul, pSuma, invMod, mod, G, N, unir};
`)(require("crypto").webcrypto, TextEncoder);

let fallos = 0;
function chequear(nombre, obtenido, esperado){
  const ok = String(obtenido).toLowerCase() === String(esperado).toLowerCase();
  if(!ok) fallos++;
  console.log(`${ok ? "OK  " : "FALLA"}  ${nombre}`);
  if(!ok){
    console.log(`        esperado: ${esperado}`);
    console.log(`        obtenido: ${obtenido}`);
  }
}

const enc = s => new TextEncoder().encode(s);

// ── keccak-256 (vectores publicados) ──
chequear("keccak256('')", C.bytesAHex(C.keccak256(enc(""))),
  "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");
chequear("keccak256('abc')", C.bytesAHex(C.keccak256(enc("abc"))),
  "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45");

// Bordes del bloque de 136 bytes, donde el padding es facil de equivocar.
chequear("keccak256(135 bytes)  [borde -1]",
  C.bytesAHex(C.keccak256(new Uint8Array(135))),
  "29e3704feeca7fb9ba229f0fa04d9b36449cf3ad6e1d85d9cfff3a10df9abc3e");
chequear("keccak256(136 bytes)  [bloque exacto]",
  C.bytesAHex(C.keccak256(new Uint8Array(136))),
  "3a5912a7c5faa06ee4fe906253e339467a9ce87d533c65be3c15cb231cdb25f9");
chequear("keccak256(137 bytes)  [borde +1]",
  C.bytesAHex(C.keccak256(new Uint8Array(137))),
  "bee7fbb405cb0d91a8775e338c4a5e4b5d6b2d051f687fa942043cffdc73bd28");
chequear("keccak256(300 bytes)  [multi-bloque]",
  C.bytesAHex(C.keccak256(new Uint8Array(300).fill(0xab))),
  "315f259936b44c2fd956d917deacbaa548f17a9d26d17df4fa2bdec09966e007");

// ── secp256k1: clave privada -> direccion (vector de EIP-155) ──
const PRIV = "4646464646464646464646464646464646464646464646464646464646464646";
chequear("direccion desde clave privada",
  C.conChecksum(C.claveADireccion(PRIV)),
  "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F");

// ── EIP-55 ──
chequear("checksum EIP-55",
  C.conChecksum("0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed"),
  "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed");

// ── RLP + hash a firmar (transaccion de ejemplo de EIP-155) ──
const campos = [
  C.bigIntABytes(9n),                      // nonce
  C.bigIntABytes(20000000000n),            // gasPrice
  C.bigIntABytes(21000n),                  // gas
  C.hexABytes("3535353535353535353535353535353535353535"),
  C.bigIntABytes(1000000000000000000n),    // value
  new Uint8Array(0),                       // data
];
const paraFirmar = C.rlp([...campos, C.bigIntABytes(1n),
                          new Uint8Array(0), new Uint8Array(0)]);
// 0xec = lista con 44 bytes de payload; 45 en total.
chequear("RLP de la tx sin firmar", C.bytesAHex(paraFirmar),
  "ec098504a817c800825208943535353535353535353535353535353535353535880de0b6b3a764000080018080");
chequear("hash a firmar (EIP-155)", C.bytesAHex(C.keccak256(paraFirmar)),
  "daf5a779ae972f972197303d7b574746c7ef83eadac0f2791ad23db92e4c8e53");

// ── la firma que producimos tiene que verificar ──
function verificar(hash, pub, r, s){
  const z = BigInt("0x" + C.bytesAHex(hash));
  const w = C.invMod(s, C.N);
  const X = C.pSuma(C.pMul(C.mod(z * w, C.N), C.G),
                    C.pMul(C.mod(r * w, C.N), pub));
  return !!X && C.mod(X.x, C.N) === r;
}
const pub = C.pMul(BigInt("0x" + PRIV), C.G);
let verifican = true, canonicas = true;
for(let i = 0; i < 10; i++){
  const h = C.keccak256(paraFirmar);
  const { r, s, v } = C.firmar(h, PRIV, 1);
  if(!verificar(h, pub, r, s)) verifican = false;
  // s en la mitad baja y v = 37/38 para chainId 1: lo que exige la red.
  if(s > C.N / 2n || (v !== 37n && v !== 38n)) canonicas = false;
}
chequear("10 firmas seguidas verifican", verifican, true);
chequear("firmas canonicas (s bajo, v correcto)", canonicas, true);

// Los campos previos a la firma son deterministas.
const firmada = C.firmarTx({nonce:9, gasPrice:20000000000n, gas:21000n,
  to:"0x3535353535353535353535353535353535353535",
  value:1000000000000000000n, data:"0x"}, PRIV, 1);
chequear("prefijo de la tx firmada", firmada.slice(0, 26), "0xf86c098504a817c800825208");

// ── muestras para el cruce con eth_account ──
const DATA = "0xa9059cbb"
  + "ed50040f721093d385a74ae4b89ebda46980d700".padStart(64, "0")
  + (250n * 10n**18n).toString(16).padStart(64, "0");
const muestras = [
  {nombre:"transferencia simple", chainId:1,
   tx:{nonce:9, gasPrice:20000000000n, gas:21000n,
       to:"0x3535353535353535353535353535353535353535",
       value:1000000000000000000n, data:"0x"}},
  {nombre:"transfer de USDL (chain 97)", chainId:97,
   tx:{nonce:0, gasPrice:3000000000n, gas:65000n,
       to:"0xc56b00e3ab3e361bf6d89dbe14e6f6bbe1be5d27", value:0n, data:DATA}},
  {nombre:"nonce y monto grandes", chainId:97,
   tx:{nonce:1234, gasPrice:5000000000n, gas:120000n,
       to:"0xc56b00e3ab3e361bf6d89dbe14e6f6bbe1be5d27", value:0n, data:DATA}},
].map(c => ({ nombre: c.nombre, chainId: c.chainId,
              esperado: C.conChecksum(C.claveADireccion(PRIV)),
              raw: C.firmarTx(c.tx, PRIV, c.chainId) }));

fs.writeFileSync(path.join(__dirname, "muestras_firmadas.json"),
                 JSON.stringify(muestras, null, 2));

console.log(fallos === 0
  ? `\nTODO OK. Escritas ${muestras.length} muestras en tools/muestras_firmadas.json`
  : `\n${fallos} FALLA(S).`);
process.exit(fallos ? 1 : 0);
