# Wealthfolio Importer

Conversor autocontenido para transformar extractos de brokers y entidades
financieras en CSV de actividades compatibles con
[Wealthfolio](https://wealthfolio.app/). Funciona como utilidad de línea de
comandos y como servicio Docker para un NAS.

La primera versión soporta:

| Fuente | Entrada | Resultado |
| --- | --- | --- |
| Revolut cuenta corriente | `account-statement_*.tsv` | Saldo inicial, pagos, ingresos, comisiones, traspasos y cambios de divisa |
| Revolut Stocks | `trading-account-statement_*.tsv` | Compras, ventas, dividendos, correcciones fiscales, depósitos y retiradas |
| Revolut Flexible Cash Funds EUR | `savings-statement_*.tsv` | Aportaciones, compras, ventas, retiradas e interés diario neto |
| Revolut Flexible Cash Funds USD | `savings-statement_*.tsv` | La misma conversión en USD |
| XTB | Exportación completa `.xlsx` | Acciones/ETF, efectivo, intereses, impuestos y resultado neto de CFD |
| Sabadell cuentas | Histórico de movimientos `.txt` | Saldo inicial, ingresos, gastos, intereses y traspasos |
| Sabadell tarjeta | Extracto de tarjeta `.txt` | Compras y devoluciones |

Fonditel está documentado como siguiente fuente, pero todavía no genera CSV.

## Seguridad y datos

El contenedor no necesita credenciales de Revolut, XTB ni Sabadell. Trabaja
únicamente con archivos exportados por el usuario. La importación automática
usa un token MCP de Wealthfolio guardado como secreto Docker y limitado a las
cuentas y actividades; nunca se incluye en la imagen ni en Git. Los extractos,
CSV generados, identificadores de cuenta y configuración privada también están
excluidos de Git.

## Uso directo

Instalación local:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Vista previa de Revolut Stocks:

```bash
wealthfolio-importer preview \
  --parser revolut-stocks \
  --currency USD \
  --input trading-account-statement.tsv
```

Conversión de un fondo flexible de Revolut:

```bash
wealthfolio-importer convert \
  --parser revolut-savings \
  --currency EUR \
  --symbol REV-CASH-EUR \
  --isin IE000AZVL3K0 \
  --input savings-statement.tsv \
  --output revolut-eur-wealthfolio.csv
```

Conversión de XTB:

```bash
wealthfolio-importer convert \
  --parser xtb \
  --currency EUR \
  --ticker-alias CSPX.UK=CSPX.L \
  --ticker-currency CSPX.UK=USD \
  --input exportacion-xtb.xlsx \
  --output xtb-wealthfolio.csv
```

`preview` ejecuta exactamente el mismo parser y las mismas conciliaciones que
`convert`, pero no escribe ningún archivo.

## Servicio Docker

Copiar la configuración de ejemplo:

```bash
mkdir -p config data/{inbox,outbox,processed,failed,state}
cp config/config.example.json config/config.json
```

La ruta de entrada identifica la configuración de la cuenta:

```text
data/inbox/
├── revolut/
│   ├── current-eur/
│   ├── stocks/
│   ├── savings-eur/
│   └── savings-usd/
├── xtb/
│   └── main/
└── sabadell/
    ├── principal/
    ├── ahorros/
    └── tarjeta/
```

Por ejemplo, un archivo colocado en `inbox/revolut/savings-eur/` usa la clave
`revolut/savings-eur` de `config.json`.

Arranque de revisión:

```bash
docker compose -f compose.example.yml up
```

El valor predeterminado `DRY_RUN=true` analiza cada archivo y muestra el
resumen, pero no escribe ni mueve nada. Después de revisar el log, cambiarlo a
`false`. El servicio entonces:

1. Convierte el archivo.
2. Escribe en `outbox/<proveedor>/<cuenta>/` únicamente las actividades que no
   hubiera emitido antes para esa cuenta.
3. Guarda los identificadores deterministas en `state/emitted.json`.
4. Mueve el original a `processed/`, o a `failed/` si no supera los controles.

El directorio `state` debe ser persistente. Borrarlo hace que la siguiente
exportación acumulativa vuelva a generar todo el histórico. El estado significa
"CSV emitido"; la importación manual en Wealthfolio sigue siendo
responsabilidad del usuario.

### Importación automática mediante MCP

Wealthfolio 3.8 incorpora un servidor MCP oficial con importación de
actividades, preview, validación y detección de duplicados. Para utilizarlo:

1. Activar `WF_MCP_ENABLED=true` y `WF_MCP_AUDIT_ENABLED=true` en el servicio
   Wealthfolio.
2. Crear en **Settings → AI Agent Access** un token con `Accounts: read`,
   `Activities: read`, `Activities: draft` y `Activities: write`.
3. Guardar el token, sin espacios ni salto adicional, en
   `/volume1/finance/wealthfolio-importer/secrets/wealthfolio_mcp_token`.
4. Rellenar `wealthfolioAccountId` en cada entrada de `config.json` con el UUID
   de la cuenta de destino.
5. Conectar ambos servicios a `traefik_proxy` y activar:

```yaml
AUTO_IMPORT: "true"
WEALTHFOLIO_MCP_URL: "http://wealthfolio:8088/mcp"
WEALTHFOLIO_MCP_TOKEN_FILE: "/run/secrets/wealthfolio_mcp_token"
```

Con `AUTO_IMPORT=true` y `DRY_RUN=true`, el servicio llama únicamente a
`prepare_activity_import`: Wealthfolio resuelve los activos, valida todas las
filas y detecta duplicados sin escribir. Con `DRY_RUN=false`, vuelve a validar
y llama a `commit_activity_import`; el estado local y el movimiento a
`processed` solo se actualizan después de que Wealthfolio confirme el commit.

El protocolo limita cada llamada a 1000 filas; el servicio usa lotes de 500 de
forma predeterminada. Wealthfolio 3.8 no permite enviar por MCP `fxRate`, `isin`
o `instrumentType`: Wealthfolio los resuelve con su histórico de divisas y el
símbolo existente. Si una actividad contiene `tax` o `subtype`, la importación
se detiene para evitar perder información estructurada.

Si el histórico ya se importó manualmente, se puede inicializar el registro sin
generar otro CSV: colocar una exportación histórica completa en cada cuenta,
usar temporalmente `DRY_RUN=false` y `SEED_STATE_ONLY=true`, esperar a que los
archivos pasen a `processed/` y volver a poner `SEED_STATE_ONLY=false`.

### Gastos y categorización

Importar actividades no incluye automáticamente una cuenta en el módulo de
gastos. Para cuentas corrientes, de ahorro y tarjetas:

1. Abrir **Settings → Spending Tracker** y mantener el tracker activado.
2. Activar cada cuenta en **Spending accounts**. El resumen debe mostrar
   `Tracking N of N cash accounts`.
3. Importar o dejar que el watcher procese los extractos.
4. Si ya había movimientos, abrir **Categorization rules → Re-run rules →
   Categorize uncategorized** para aplicar las reglas sin sobrescribir las
   categorías manuales.

Wealthfolio aplica las reglas al texto visible en **Name / Notes**. Los parsers
conservan ahí el concepto bancario o el comercio mediante `comment`, por lo que
no hace falta una columna adicional para categorizar. Los movimientos que sigan
como `UNCATEGORIZED` no coinciden con ninguna regla configurada y requieren una
regla nueva o una categoría manual.

## Portainer

`portainer-stack.yml` usa la imagen
`ghcr.io/gummiees/wealthfolio-importer:latest` y rutas bajo
`/volume1/finance/wealthfolio-importer`. Crear esas carpetas en el NAS, copiar
allí `config/config.json`, pegar el stack en Portainer. El despliegue activo usa `AUTO_IMPORT=true` y
`DRY_RUN=false`; para una instalación nueva conviene validar primero con
`DRY_RUN=true` y cambiarlo a `false` después de comprobar el preview.

La acción de GitHub incluida publica `latest`, la etiqueta de versión y una
etiqueta por commit cuando se hace push a `main` o a una etiqueta `v*`.

## Formato de salida

Todos los conversores escriben la misma cabecera:

```text
date,symbol,instrumentType,isin,quantity,activityType,unitPrice,
currency,fee,tax,amount,fxRate,subtype,comment
```

Los comentarios contienen referencias deterministas. La deduplicación del
servicio usa el registro persistente y no depende del comportamiento de
Wealthfolio al reimportar un CSV.

## Reglas de Sabadell

Las cuentas principal y de ahorros usan el TXT de siete columnas que exporta
Sabadell: fecha de operación, concepto, fecha valor, importe, saldo, NIF y
referencia. La tarjeta usa el TXT del extracto, con filas de fecha `DD/MM`,
concepto, localidad e importe. El nombre del fichero de tarjeta debe contener
una fecha `DDMMYYYY`, de la que se obtiene el año.

- En el primer extracto de una cuenta, el parser calcula el saldo inicial a
  partir del movimiento más antiguo y su saldo resultante. Esa actividad tiene
  un identificador estable por cuenta, de modo que los siguientes extractos no
  vuelven a crearla. Se puede desactivar con `includeOpeningBalance: false` o
  fijar manualmente `openingBalance` y `openingDate`.
- Los abonos y cargos se convierten en `DEPOSIT` y `WITHDRAWAL`; la remuneración
  de la cuenta se registra como `INTEREST` y las comisiones como `FEE`.
- Los conceptos `TRASPASO` y `TARJETA CREDITO` se registran como
  `TRANSFER_IN`/`TRANSFER_OUT`. `transferRules` permite identificar el destino.
  Con `mirror: true`, el mismo fichero genera además la contrapartida en la
  cuenta indicada por `targetAccount`; el servicio agrupa las actividades y
  llama a MCP con el UUID correcto para cada cuenta.
- No se debe activar `mirror` cuando se vayan a importar los extractos de ambos
  lados del traspaso: cada extracto ya contiene su propia actividad. La
  configuración de ejemplo solo lo activa para la liquidación mensual de la
  tarjeta, ya que el extracto de tarjeta contiene compras y devoluciones.
- En el extracto de tarjeta, los importes positivos son compras y producen
  `WITHDRAWAL`; los negativos son devoluciones y producen `CREDIT`.
- La deduplicación usa todos los campos originales de cada movimiento. Un
  extracto acumulativo o solapado puede depositarse directamente en `inbox`:
  solo se envían las filas nuevas.
- El comentario enviado a Wealthfolio incluye el saldo posterior del extracto.
  Esto distingue cargos legítimos con la misma fecha, importe y descripción,
  que el detector MCP consideraría duplicados si las filas fueran idénticas.

## Reglas de Revolut Stocks

- `CASH TOP-UP` y `CASH WITHDRAWAL` producen `DEPOSIT` y `WITHDRAWAL`.
- `BUY - MARKET` y `SELL - MARKET` producen `BUY` y `SELL`.
- `DIVIDEND` produce `DIVIDEND`.
- Una corrección fiscal negativa produce `TAX`; una positiva produce `CREDIT`
  con subtipo `REIMBURSEMENT`.
- El `FX Rate` del TSV se invierte porque Revolut lo expresa como unidades de
  la divisa del activo por EUR, mientras el CSV necesita EUR por unidad de la
  divisa del activo.
- Cualquier tipo nuevo detiene la conversión para evitar una clasificación
  silenciosa incorrecta.

## Reglas de Revolut cuenta corriente

El extracto se obtiene en Revolut como TSV de la cuenta corriente. Para la
cuenta EUR se deposita directamente en `inbox/revolut/current-eur/`.

- Solo se importan filas con estado `COMPLETADO`/`COMPLETED`; las pendientes o
  rechazadas se omiten y se muestran en los avisos del preview.
- En el primer extracto se calcula el saldo inicial desde el primer movimiento.
  Su identificador es estable por cuenta, de modo que los extractos solapados
  posteriores no vuelven a crearlo.
- Pagos con tarjeta, pagos de Revolut y transferencias externas son gastos o
  ingresos (`WITHDRAWAL`, `DEPOSIT` o `CREDIT`) y conservan el comercio o
  destinatario en las notas para la categorización.
- Los movimientos desde/hacia `EUR Ahorro` y los cambios de divisa se registran
  como `TRANSFER_IN`/`TRANSFER_OUT`; así no cuentan como gasto ni ingreso.
- Las comisiones se registran por separado como `FEE`. Cada fila se reconcilia
  con el saldo posterior antes de generar o importar actividades.
- Las fechas se envían como `YYYY-MM-DD`: Wealthfolio 3.8 rechaza en el commit
  las horas locales sin zona que incluye el TSV, aunque su preview las acepte.
- La deduplicación incluye fecha, tipo, descripción, importes, divisa y saldo.
  Se puede depositar cada nuevo extracto sin recortarlo manualmente.
- Un tipo de operación desconocido detiene la conversión para evitar una
  clasificación silenciosa incorrecta.

## Reglas de Revolut Flexible Cash Funds

Configuración recomendada en Wealthfolio: cuenta `Securities`, divisa EUR o
USD y tracking mode `Transactions`. Los activos analizados son
`IE000AZVL3K0` para EUR y `IE000H9J0QX4` para USD, ambos con precio unitario 1.

- Cada pareja `Return PAID` y `Service Fee Charged` genera un único `INTEREST`
  neto.
- `Return Reinvested` se empareja con su `BUY`; ese `BUY` consume el efectivo
  generado por los intereses y no crea otro depósito.
- Por defecto, un `BUY` externo genera `DEPOSIT` un segundo antes y después `BUY`;
  un `SELL` genera `SELL` y después `WITHDRAWAL`. `Return WITHDRAWN`, cuando
  existe, se añade a la retirada.
- Para cuentas de ahorro financiadas desde otra cuenta ya importada, activa
  `externalTransfers: true`: esas mismas entradas y salidas pasan a ser
  `TRANSFER_IN` y `TRANSFER_OUT`, para que Wealthfolio pueda vincularlas.
- Las fechas aceptan abreviaturas españolas e inglesas.
- El parser trata explícitamente las abreviaturas ambiguas del TSV español:
  `4.5` en una operación externa representa 4.500. Para un entero pequeño sin
  separador, compara el rendimiento diario anterior y posterior para decidir
  si representa unidades o miles, e informa de cada decisión en el resumen.
- Si la serie no permite distinguirlo de forma segura, la conversión se
  detiene. La cuenta puede resolver el caso con `amountOverrides`, usando una
  clave como `2024-10-07T12:36:03|BUY` y el importe correcto como valor.
- Las reinversiones y rendimientos conservan su escala propia.
- Se comprueban el efectivo y las participaciones finales; una posición o un
  efectivo negativos detienen la conversión.

## Reglas de XTB

La exportación completa contiene `Closed Positions`, `Cash Operations` y
`Open Positions`. Los tres botones de exportación de XTB generan el mismo
libro; basta con usar uno.

- `Cash Operations` es la fuente principal y su ID identifica cada movimiento.
- Las compras y ventas extraen cantidad y precio del comentario `OPEN/CLOSE
  BUY ... @ ...`.
- Las fracciones de una misma orden se conservan como filas separadas.
- Los traspasos entre `My Trades` e `Investment Plans` se omiten porque la
  configuración actual consolida ambos productos en una cuenta Wealthfolio.
- `Close trade`, `Swap` y `Rollover` se agrupan por posición CFD. Solo se
  importa el resultado realizado neto como `CREDIT` o `FEE`.
- Los aliases y divisas especiales de ticker se configuran por cuenta. El caso
  conocido es `CSPX.UK` → `CSPX.L`, denominado en USD.
- El total de efectivo debe coincidir con la fila `Total` y las cantidades
  históricas deben reconstruir exactamente las posiciones abiertas.

## Fonditel Alfa

Fonditel Alfa (`F0635`) se seguirá en Wealthfolio mediante una cuenta
`Securities` con tracking mode `Holdings`. `Valor liquidativo.xlsx` aporta la
serie de precios, pero no las aportaciones ni la distribución geográfica. Los
gastos ya están incorporados en el valor liquidativo y no deben importarse de
nuevo como `FEE`.

El importador de Fonditel queda pendiente de una fuente estructurada de
movimientos o de definir formalmente el formato de snapshots periódicos.

## Desarrollo

```bash
python -m unittest discover -s tests -v
docker build -t wealthfolio-importer:test .
```

Los tests usan extractos mínimos ficticios. Los archivos financieros reales no
forman parte del repositorio.
