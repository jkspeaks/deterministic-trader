# Rotation codes

Short codes for `sectors`. Generated from `config/rotation_codes.json`, which is the source of truth; `tests/test_rotation_codes.py` fails if this page and the config disagree.

A stock passes the rotation when its **sector or its GICS sub-industry** is active. A sector ETF (XLK…) passes only when its whole sector is active. Codes, full names and sleeve tickers (XLE) are all accepted, case-insensitive, comma-separated: `sectors set SEMI, SOFT, DC`. Membership comes from the Wikipedia S&P 500 table the weekly Universe job fetches.

## Sectors

| Code | Sector |
|---|---|
| `EN` | Energy |
| `FIN` | Financials |
| `IT` | Information Technology |
| `HC` | Health Care |
| `IND` | Industrials |
| `CD` | Consumer Discretionary |
| `CS` | Consumer Staples |
| `UTL` | Utilities |
| `MAT` | Materials |
| `RE` | Real Estate |
| `COM` | Communication Services |

## Baskets

| Code | Basket | Includes |
|---|---|---|
| `SEMI` | Semiconductors + equipment | `CHIP` Semiconductors, `SME` Semiconductor Materials & Equipment |
| `SOFT` | Software | `APPS` Application Software, `SYSW` Systems Software |
| `BANK` | Banks | `DBNK` Diversified Banks, `RBNK` Regional Banks |
| `INS` | Insurance | `LIFE` Life & Health Insurance, `MINS` Multi-line Insurance, `PC` Property & Casualty Insurance, `REIN` Reinsurance, `IBRK` Insurance Brokers |
| `RESI` | Residential REITs | `MFR` Multi-Family Residential REITs, `SFR` Single-Family Residential REITs |

## Sub-industries

### Energy (`EN`)

| Code | Sub-industry |
|---|---|
| `IOG` | Integrated Oil & Gas |
| `OFS` | Oil & Gas Equipment & Services |
| `EP` | Oil & Gas Exploration & Production |
| `REF` | Oil & Gas Refining & Marketing |
| `MID` | Oil & Gas Storage & Transportation |

### Financials (`FIN`)

| Code | Sub-industry |
|---|---|
| `AMGT` | Asset Management & Custody Banks |
| `CFIN` | Consumer Finance |
| `DBNK` | Diversified Banks |
| `EXCH` | Financial Exchanges & Data |
| `IBRK` | Insurance Brokers |
| `IBNK` | Investment Banking & Brokerage |
| `LIFE` | Life & Health Insurance |
| `HOLD` | Multi-Sector Holdings |
| `MINS` | Multi-line Insurance |
| `PC` | Property & Casualty Insurance |
| `RBNK` | Regional Banks |
| `REIN` | Reinsurance |
| `PAY` | Transaction & Payment Processing Services |

### Information Technology (`IT`)

| Code | Sub-industry |
|---|---|
| `APPS` | Application Software |
| `CEQ` | Communications Equipment |
| `ECMP` | Electronic Components |
| `EEI` | Electronic Equipment & Instruments |
| `EMS` | Electronic Manufacturing Services |
| `ITCS` | IT Consulting & Other Services |
| `INET` | Internet Services & Infrastructure |
| `SME` | Semiconductor Materials & Equipment |
| `CHIP` | Semiconductors |
| `SYSW` | Systems Software |
| `TDIS` | Technology Distributors |
| `HW` | Technology Hardware, Storage & Peripherals |

### Health Care (`HC`)

| Code | Sub-industry |
|---|---|
| `BIO` | Biotechnology |
| `HCD` | Health Care Distributors |
| `MEDT` | Health Care Equipment |
| `HCF` | Health Care Facilities |
| `HCS` | Health Care Services |
| `HSUP` | Health Care Supplies |
| `HCIT` | Health Care Technology |
| `LSTS` | Life Sciences Tools & Services |
| `MCO` | Managed Health Care |
| `PHRM` | Pharmaceuticals |

### Industrials (`IND`)

| Code | Sub-industry |
|---|---|
| `AERO` | Aerospace & Defense |
| `FARM` | Agricultural & Farm Machinery |
| `FRGT` | Air Freight & Logistics |
| `BLDG` | Building Products |
| `TRCK` | Cargo Ground Transportation |
| `CENG` | Construction & Engineering |
| `HVY` | Construction Machinery & Heavy Transportation Equipment |
| `DPOS` | Data Processing & Outsourced Services |
| `DSS` | Diversified Support Services |
| `ELEQ` | Electrical Components & Equipment |
| `ENVS` | Environmental & Facilities Services |
| `HEEQ` | Heavy Electrical Equipment |
| `HR` | Human Resource & Employment Services |
| `CONG` | Industrial Conglomerates |
| `MACH` | Industrial Machinery & Supplies & Components |
| `AIR` | Passenger Airlines |
| `PGT` | Passenger Ground Transportation |
| `RAIL` | Rail Transportation |
| `RCS` | Research & Consulting Services |
| `TCD` | Trading Companies & Distributors |

### Consumer Discretionary (`CD`)

| Code | Sub-industry |
|---|---|
| `APRL` | Apparel Retail |
| `LUX` | Apparel, Accessories & Luxury Goods |
| `AUTO` | Automobile Manufacturers |
| `APTS` | Automotive Parts & Equipment |
| `ARTL` | Automotive Retail |
| `BRTL` | Broadline Retail |
| `CASI` | Casinos & Gaming |
| `ERTL` | Computer & Electronics Retail |
| `CELE` | Consumer Electronics |
| `DIST` | Distributors |
| `SHOE` | Footwear |
| `HIRT` | Home Improvement Retail |
| `HOME` | Homebuilding |
| `HFRT` | Homefurnishing Retail |
| `HOTL` | Hotels, Resorts & Cruise Lines |
| `LEIS` | Leisure Products |
| `SRTL` | Other Specialty Retail |
| `REST` | Restaurants |
| `SVCS` | Specialized Consumer Services |

### Consumer Staples (`CS`)

| Code | Sub-industry |
|---|---|
| `AGRI` | Agricultural Products & Services |
| `CSRT` | Consumer Staples Merchandise Retail |
| `ALC` | Distillers & Vintners |
| `FDIS` | Food Distributors |
| `FRTL` | Food Retail |
| `HHP` | Household Products |
| `FOOD` | Packaged Foods & Meats |
| `PCP` | Personal Care Products |
| `BEV` | Soft Drinks & Non-alcoholic Beverages |
| `TOB` | Tobacco |

### Utilities (`UTL`)

| Code | Sub-industry |
|---|---|
| `ELEC` | Electric Utilities |
| `GUTL` | Gas Utilities |
| `IPP` | Independent Power Producers & Energy Traders |
| `MULT` | Multi-Utilities |
| `WATR` | Water Utilities |

### Materials (`MAT`)

| Code | Sub-industry |
|---|---|
| `CCHM` | Commodity Chemicals |
| `CMAT` | Construction Materials |
| `CU` | Copper |
| `FERT` | Fertilizers & Agricultural Chemicals |
| `GOLD` | Gold |
| `IGAS` | Industrial Gases |
| `CONT` | Metal, Glass & Plastic Containers |
| `PACK` | Paper & Plastic Packaging Products & Materials |
| `SCHM` | Specialty Chemicals |
| `STL` | Steel |

### Real Estate (`RE`)

| Code | Sub-industry |
|---|---|
| `DC` | Data Center REITs |
| `HCRE` | Health Care REITs |
| `HTRE` | Hotel & Resort REITs |
| `INRE` | Industrial REITs |
| `MFR` | Multi-Family Residential REITs |
| `OFFC` | Office REITs |
| `SPRE` | Other Specialized REITs |
| `RESV` | Real Estate Services |
| `RTRE` | Retail REITs |
| `STOR` | Self-Storage REITs |
| `SFR` | Single-Family Residential REITs |
| `TOWR` | Telecom Tower REITs |
| `TIMB` | Timber REITs |

### Communication Services (`COM`)

| Code | Sub-industry |
|---|---|
| `ADV` | Advertising |
| `BCST` | Broadcasting |
| `CABL` | Cable & Satellite |
| `TELC` | Integrated Telecommunication Services |
| `GAME` | Interactive Home Entertainment |
| `IMED` | Interactive Media & Services |
| `FILM` | Movies & Entertainment |
| `PUB` | Publishing |
| `WIRE` | Wireless Telecommunication Services |
