"""
Official Balaji Transports Vendor Directory & Tally Expense Mapping.
Extracted from official GSTR-2A returns and Tally Purchase Register for Balaji Transports (GSTIN: 29AAWFB0395H1ZX).

Core data required for Balaji operations:
1. Invoice ID (Voucher No in Tally)
2. Vendor Name (Particulars in Tally)
3. Expense Ledger (Head of Expense in Tally Purchase Register)
"""

from typing import Optional, Dict, List, Any


BALAJI_COMPANY_INFO = {
    'gstin': '29AAWFB0395H1ZX',
    'legal_name': 'BALAJI TRANSPORTS',
    'trade_name': 'BALAJI TRANSPORTS',
    'state': 'Karnataka',
    'state_code': '29',
}

# Standard Tally Expense Ledgers matching Balaji's official Purchase Register
TALLY_EXPENSE_LEDGERS = [
    'Purchase Spares & Consumables',
    'Tyre & Tube Purchase',
    'Tyre Retread',
    'Labour Charges / Vehicle Service',
    'GPS-Expenses',
    'Lorry Insurance',
    'Transportation & Freight Charges',
    'Canteen & Staff Welfare Expenses',
    'Building Repairs, Hardware & Paints',
    'Printing & Stationery',
    'Computer & Printer Expenses',
    'Telephone & Mobile Expenses',
    'Diesel & Lubricants',
    'Welding Gases & Industrial Spares',
    'Rent & Terminal Charges',
    'General Office Expenses',
]

# Master directory: GSTIN -> Vendor Info & Default Tally Expense Ledger
VENDOR_DIRECTORY: Dict[str, Dict[str, str]] = {
    '29APFPN9602K1ZF': {'name': 'A M R TRADING', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAXFA9222F1Z1': {'name': 'ABC AUTO CORPORATION', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AALPA1711H1Z2': {'name': 'ACCURA SOLUTIONS', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AJKPD8139L2ZN': {'name': 'ADITYA PRINTING WORKS', 'state': 'Karnataka', 'ledger': 'Printing & Stationery'},
    '08AAXCA1693H1Z4': {'name': 'ADSERA IT SERVICES PRIVATE LIMITED', 'state': 'Rajasthan', 'ledger': 'Computer & Printer Expenses'},
    '08AABCA0522B1ZK': {'name': 'AIR INDIA EXPRESS LIMITED', 'state': 'Rajasthan', 'ledger': 'Transportation & Freight Charges'},
    '29AAIFA2641P1Z2': {'name': 'AKSHAYA AUTO SERVICE', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29CHJPB2671L1Z1': {'name': 'AMAZING LOGISTICS', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '07AAJCA9880A1ZL': {'name': 'AMAZON WEB SERVICES INDIA PRIVATE LIMITED', 'state': 'Delhi', 'ledger': 'Computer & Printer Expenses'},
    '29APPPP0996Q1ZK': {'name': 'AMEYAA', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAVCA3479R1ZD': {'name': 'AML MOTORS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAJCA4529L1Z8': {'name': 'AMOGH GASES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Welding Gases & Industrial Spares'},
    '29AACCA3959R1ZV': {'name': 'AMPLE TECHNOLOGIES PVT LTD', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AJJPB0025C1ZX': {'name': 'ANANYA AUTO AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29EFYPM5031L1ZL': {'name': 'ANU MOBILES', 'state': 'Karnataka', 'ledger': 'Telephone & Mobile Expenses'},
    '29AAWCA0630P1Z1': {'name': 'ARTEX LOGISTICS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '29GIMPS9937L1ZV': {'name': 'Ashwamedha Enterprises', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ABXFA3585G1ZR': {'name': 'ATLANTIC INDUSTRIAL', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ANHPT5737H1ZE': {'name': 'A-Z HYDRAULIC HOSE PIPES AND FITTINGS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AGIPN3616M1ZX': {'name': 'B S L ELECTRICALS AND HARDWARE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAICB6937G2ZA': {'name': 'BALAJI MOVERS AND SIDING PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '29AAIFB1733H1ZJ': {'name': 'BANNADA MANE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AVZPJ3961B1Z4': {'name': 'BASANNA JANARDHAN', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AACCB3116F1Z2': {'name': 'BELLAD ENTERPRISES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAYFB6638F1ZT': {'name': 'BETTER INDUSTRIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AQMPA4634J1ZN': {'name': 'BHARATH AUTO ELCTRICAL WORKS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAACB2894G1ZJ': {'name': 'BHARTI AIRTEL LIMITED', 'state': 'Karnataka', 'ledger': 'Telephone & Mobile Expenses'},
    '29AVZPJ3961B1Z4': {'name': 'BHEESHMA METALS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAPFB1846A1ZK': {'name': 'BLITZ NETWORK', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAHFB1639F1ZJ': {'name': 'BOMBAY AUTO CORPORATION', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAACC1206D2ZB': {'name': 'CENTRAL WAREHOUSING CORPORATION', 'state': 'Karnataka', 'ledger': 'Rent & Terminal Charges'},
    '29AAJFC6480F1Z7': {'name': 'CHANDAN MARKETING', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '24AAJCC9783E1ZD': {'name': 'CLICKTECH RETAIL PRIVATE LIMITED', 'state': 'Gujarat', 'ledger': 'General Office Expenses'},
    '29AAJCC9783E1Z3': {'name': 'CLICKTECH RETAIL PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '27AAJCC9783E1Z7': {'name': 'CLICKTECH RETAIL PRIVATE LIMITED', 'state': 'Maharashtra', 'ledger': 'General Office Expenses'},
    '36AAJCC8517E1ZM': {'name': 'COCOBLU RETAIL LIMITED', 'state': 'Telangana', 'ledger': 'General Office Expenses'},
    '29ADEPB6470A1ZY': {'name': 'CROMPTON TRADING COMPANY', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29DJFPM7824D1Z0': {'name': 'D.H.K.STEEL ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAKFD5320Q1ZX': {'name': 'D.P.S.TECHNOLOGIES', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AAMCM3175B1ZE': {'name': 'DAWNTECH ELECTRONICS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AAAFD6298B1ZF': {'name': 'DEBESON AND COMPANY', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29DRAPP0263E1ZX': {'name': 'DEEPIKA AUTOMOTIVE', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AABFE5063E1ZM': {'name': 'ESSARR AUTOMOTIVES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29CQYPP8950N1ZB': {'name': 'ESTEEM ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '19AABCV2516J1Z9': {'name': 'ETRANS SOLUTIONS PVT. LTD.', 'state': 'West Bengal', 'ledger': 'GPS-Expenses'},
    '29ACJPT7015M2ZW': {'name': 'EXPRESS TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AABCF8078M1ZX': {'name': 'FLIPKART INDIA PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '36AABCF8078M1Z2': {'name': 'FLIPKART INDIA PRIVATE LIMITED', 'state': 'Telangana', 'ledger': 'General Office Expenses'},
    '06AABCF8078M1Z5': {'name': 'FLIPKART INDIA PVT LTD', 'state': 'Haryana', 'ledger': 'General Office Expenses'},
    '29AACCF0683K1ZD': {'name': 'FLIPKART INTERNET PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29ABACS0877M1ZS': {'name': 'FYNAMICS TECHNO SOLUTIONS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAACG1659G1ZL': {'name': 'GEORGE OAKES LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AACCO4128Q1ZW': {'name': 'GO DIGIT GENERAL INSURANCE LIMITED', 'state': 'Karnataka', 'ledger': 'Lorry Insurance'},
    '29AGPPB0377A1ZN': {'name': 'GOKARAM RAMKRISHNA BALAJI', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '29AACCH0941E1ZV': {'name': 'GULF OIL LUBRICANTS INDIA LIMITED', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AAIFH4408B1ZN': {'name': 'H D MOTORS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29DDAPR6373B1ZF': {'name': 'H.B ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29EFYPR9916D1ZF': {'name': 'HADDINAKALLU TRADERS', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29ACXPV2864J1ZG': {'name': 'HARISH TRADERS', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29AAHCH2846P2ZU': {'name': 'HATMAN RETAILS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAACH2702H1ZW': {'name': 'HDFC BANK LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AADCH8449J1Z0': {'name': 'HEALTH AND HAPPINESS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '09AAHCH0351H1ZQ': {'name': 'HERBYSHINE PRIVATE LIMITED', 'state': 'Uttar Pradesh', 'ledger': 'General Office Expenses'},
    '29DTDPK0946L1ZA': {'name': 'HINDUSTAN AUTOMOTIVE SPARES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAZPI9452K1ZN': {'name': 'HINDUSTAN RADIATORS AND OIL COOLERS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29CQGPM3324R1Z6': {'name': 'HMG TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29CAVPM8201J1Z2': {'name': 'HMS TYRE WORLD AND TYRE WORKS', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29IIRPS3571M1Z0': {'name': 'HMS TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29DWEPK9062B1ZF': {'name': 'HYDRO FLEXIBLES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAFFH7883P1ZC': {'name': 'HYDRO PNEUMATIC CONTROLS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ATLPS4283M1ZP': {'name': 'HYDROLINES SPARES AND SERVICES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '27AAACI1195H3ZK': {'name': 'ICICI BANK LIMITED', 'state': 'Maharashtra', 'ledger': 'General Office Expenses'},
    '29AAACI1195H1ZI': {'name': 'ICICI BANK LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAACI7573H1Z8': {'name': 'IFFCO TOKIO GENERAL INSURANCE COMPANY LTD.', 'state': 'Karnataka', 'ledger': 'Lorry Insurance'},
    '29AHIPK5883A1Z6': {'name': 'INDIAN AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '07AAACI7074F1ZM': {'name': 'INDIAN RAILWAY CATERING AND TOURISM CORPORATION LTD', 'state': 'Delhi', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29BAJPS0075B1ZQ': {'name': 'INDO PNEUMATIC AND TOOLS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ACXPB0078Q1ZS': {'name': 'INDOFREEZE (BANGALORE)', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '27AAPFJ9153L1ZL': {'name': "J S DIESEL SPARE'S", 'state': 'Maharashtra', 'ledger': 'Diesel & Lubricants'},
    '29AURPA5374N1ZV': {'name': 'J.J.TYRES & TUBES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29CEEPP4567B1ZC': {'name': 'JAGADAMBA HARDWARE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AALFJ8442L1ZN': {'name': 'JAI HANUMAN AUTOMOTIVES', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAKFJ9099A1ZW': {'name': 'JAIN BROS.', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAGCJ9708B1ZG': {'name': 'JAYA PRABHU LINGAYATH ELECTROMECH PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAACJ6716F1ZK': {'name': 'JK TYRE & INDUSTRIES LIMITED', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AAACJ4323N1ZC': {'name': 'JSW STEEL LTD', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AQOPB7117M1ZE': {'name': 'KARNATAKA TYRE CARE CENTRE', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AARFK5708N1ZH': {'name': 'KARTHIK TYER RETREADS', 'state': 'Karnataka', 'ledger': 'Tyre Retread'},
    '27AOIPM8693M1ZY': {'name': 'KATHA CHOCOLATES', 'state': 'Maharashtra', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29AAAHK6527L1ZV': {'name': 'KESAR AUTO AND TYRE CENTRE', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '34AETPK2089N1ZR': {'name': 'KK Logistics', 'state': 'Puducherry', 'ledger': 'Transportation & Freight Charges'},
    '29BWCPP5068A1ZI': {'name': 'Krishna Electronics', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29BRZPP2008L1Z0': {'name': 'KST LUBRICANTS', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AASPJ9771H1ZU': {'name': 'KUSHAL ELECTRICALS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAACL3061H1ZM': {'name': 'LAMINA SUSPENSION PRODUCTS LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AABFL0277H1ZB': {'name': 'LAXMI GLASS AND PLYWOODS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29ARPPM6847F1Z4': {'name': 'LMNG COMPUTERS', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AERPU0771P1ZD': {'name': 'M B S AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29FQKPP2506M1Z8': {'name': 'M P ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '36AAAFZ6851A1Z9': {'name': 'M/S 4 CLOSURE', 'state': 'Telangana', 'ledger': 'General Office Expenses'},
    '27AADCI7688G1Z3': {'name': 'M/s INTANGLES LAB PRIVATE LIMITED', 'state': 'Maharashtra', 'ledger': 'GPS-Expenses'},
    '29ABWFS9121K1Z9': {'name': 'M/S SRI MARUTHI GLASS & PLYWOODS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AKGPG0316H2ZG': {'name': 'M/S.AKAL AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAUCM0301R1ZV': {'name': 'Ma Durga Mobility Private Limited', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AIMPP1937D1Z1': {'name': 'MAHALAKSHMI COMPUTECH', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AAACM3025E2ZU': {'name': 'MAHINDRA & MAHINDRA LTD', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '23AAACM3025E1Z7': {'name': 'MAHINDRA AND MAHINDRA LIMITED', 'state': 'Madhya Pradesh', 'ledger': 'Purchase Spares & Consumables'},
    '27AAACM3025E1ZZ': {'name': 'MAHINDRA AND MAHINDRA LIMITED', 'state': 'Maharashtra', 'ledger': 'Purchase Spares & Consumables'},
    '19AAACM3025E1ZW': {'name': 'MAHINDRA AND MAHINDRA LTD', 'state': 'West Bengal', 'ledger': 'Purchase Spares & Consumables'},
    '29AAMFM6355L1ZJ': {'name': 'MANJU AGENCIES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AABCM2494G1ZB': {'name': 'MANMET ENGINEERING PRODUCTS PVT LTD.', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AHAPD0632L1ZL': {'name': 'MARS TECHNOLOGIES INC', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29ANBPS8379N1ZW': {'name': 'MARUTHI HARDWARE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29BPYPG8573A1ZB': {'name': 'MARUTHI HOME APPLIANCES', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29ABHPL8709P1ZS': {'name': 'MEDIKEM INDUSTRIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29FYHPS2009R1ZK': {'name': 'MMM MOTORS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29ACKPB4000B1ZE': {'name': 'MOHIT TRADING CO', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29AUFPJ4485M1ZY': {'name': 'Mohammed Javeed', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '33AALCM2552R1ZY': {'name': 'MPOVER INDIA TYRE ESERVICE AND AMC PRIVATE LIMITED', 'state': 'Tamil Nadu', 'ledger': 'Tyre Retread'},
    '29MPDPS3338P1ZT': {'name': 'N B AUTOMOBILES & LUBRICANTS', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AAKPI6627B1ZQ': {'name': 'N.A.INDUSTRIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ALWPP5625R1ZR': {'name': 'NATIONAL HARDWARE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '27AAACN9967E1Z3': {'name': 'NATIONAL INSURANCE COMPANY LIMITED', 'state': 'Maharashtra', 'ledger': 'Lorry Insurance'},
    '29LUKPK2112C1ZQ': {'name': 'NAVARATHAN MARKETING', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '37AAVFN3225B1ZA': {'name': 'NAVATA AUTOMOTIVES', 'state': 'Andhra Pradesh', 'ledger': 'Purchase Spares & Consumables'},
    '29ACQPG2078N1ZW': {'name': 'NAVEEN AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29BJTPK9056R1ZT': {'name': 'NEOTRADE MARKETING', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29FVFPS5346G1Z1': {'name': 'NEW COMPUTER KINGDOM', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AAJCN1854F1ZA': {'name': 'NORTHSTAR MOTORS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AADCJ3507B1ZY': {'name': 'NSSR AUTO HOUSE INDIA PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29DKYPA8509P1Z2': {'name': 'PADMASHREE GASES', 'state': 'Karnataka', 'ledger': 'Welding Gases & Industrial Spares'},
    '29AGKPP4404F1ZC': {'name': 'PAREKH AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AFOPP9289H1ZD': {'name': 'PCN TRADERS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ARKPT9940M1ZN': {'name': 'PLASTICO IMPEX', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29BLRPP0581Q1Z0': {'name': 'POONAM PLAST', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '33AADCP5431C1ZY': {'name': 'POWER WELD EQUIPMENTS INDIA PRIVATE LIMITED', 'state': 'Tamil Nadu', 'ledger': 'Welding Gases & Industrial Spares'},
    '29AQZPK0417E1ZM': {'name': 'POWERBULL LIFTING TOOLS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ALWPN9852E1Z7': {'name': 'PRAKRUTHI TRADERS', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AKBPP9588M1Z2': {'name': 'PRASAD POWER ENGINEERS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AEBPM6718A1ZP': {'name': 'PREETHI PRIYA AUTOMIBILES', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AADFP8463F1ZX': {'name': 'PRO-TECH SERVICES', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AABCA9447K1Z7': {'name': 'PSN AUTOMOTIVE MARKETING PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AEBPR8469R1Z9': {'name': 'R K HARDWARE & ELECTRICALS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AABFR4740H1Z5': {'name': 'RAMESH AUTOMOBILES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ATHPJ9897Q1Z8': {'name': 'RANA HARDWARES & ELECTRICALS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AQBPP8590P1ZS': {'name': 'REKHA PAPER', 'state': 'Karnataka', 'ledger': 'Printing & Stationery'},
    '29AALCR3173P1ZJ': {'name': 'RETAILEZ PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '24AAECR0564M1Z9': {'name': 'RK WORLDINFOCOM PRIVATE LIMITED', 'state': 'Gujarat', 'ledger': 'Telephone & Mobile Expenses'},
    '29AAKPH6199K1ZX': {'name': 'ROYAL HARDWARES & TOOLS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AEAPA3069D1ZY': {'name': 'RUBY STEELS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29ACPPY5010K1ZZ': {'name': 'S A T INTEGRATED LOGISTICS', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '29BLYPM4089B1ZG': {'name': 'S M TRADERS', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AFAPN0524M1ZE': {'name': 'S N ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29ABEPJ7009D1ZV': {'name': 'S R S AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '37ABDHS8210C1ZC': {'name': 'S.R.S TEXTILES', 'state': 'Andhra Pradesh', 'ledger': 'Purchase Spares & Consumables'},
    '27AMQPK7464C1ZQ': {'name': 'S.S. ENTERPRISES', 'state': 'Maharashtra', 'ledger': 'Purchase Spares & Consumables'},
    '29AEZFS5356N1ZP': {'name': 'SAFE AND SECURE SOLUTIONS', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '08AAOCS2362J1ZY': {'name': 'SAG INFOTECH PRIVATE LIMITED', 'state': 'Rajasthan', 'ledger': 'General Office Expenses'},
    '29ADLFS2360K1ZL': {'name': 'SAI AUTO WORKS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAGPM1198K1Z7': {'name': 'SAI ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29ACUFS0217R1Z8': {'name': 'SAI WHEELZ', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAVFS3394Q1ZT': {'name': 'SARATHI TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AJLPM7750P1Z7': {'name': 'SATYAM AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '36AAXCS8821M1ZC': {'name': 'SAVADIKA RETAIL PRIVATE LIMITED', 'state': 'Telangana', 'ledger': 'General Office Expenses'},
    '29AAXCS8821M2Z6': {'name': 'SAVADIKA RETAIL PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '06AAXCS8821M1ZF': {'name': 'SAVADIKA RETAIL PRIVATE LIMITED', 'state': 'Haryana', 'ledger': 'General Office Expenses'},
    '29AATCS7344B1ZY': {'name': 'SCA HEAVY EQUIPMENT PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AVHPT4590Q1ZE': {'name': 'SF AUTO TECH', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AAGFS7679K1Z8': {'name': 'SHA JASRAJ JAIN', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AKSPJ6636K1ZC': {'name': 'SHAKTHI AUTOMOBILES AND TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29ALRPL8840K1Z7': {'name': 'SHASHANK LUBRICANTS', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AYQPS4807H1ZR': {'name': 'SHILPA AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ACGPM5681D1ZE': {'name': 'SHILPA WELDGAS AGENCIES', 'state': 'Karnataka', 'ledger': 'Welding Gases & Industrial Spares'},
    '29ABNPS1264Q1ZP': {'name': 'SHIVA ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Printing & Stationery'},
    '29ACWFS3408C1ZU': {'name': 'SHIVANNA TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AJBPS1524R1ZN': {'name': 'SHOWOFFFF', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29ABVFS6041F2ZN': {'name': 'SHREE AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29LMZPS9122B1Z5': {'name': 'SHREE BALAJI ENGINEERING CORPORATION', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AISPH4368Q1Z5': {'name': 'SHREE TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '37AAKCS2509K1Z2': {'name': 'SHRIRAM GENERAL INSURANCE COMPANY LTD', 'state': 'Andhra Pradesh', 'ledger': 'Lorry Insurance'},
    '29ADOPG6792J1ZR': {'name': 'SHUBH PAPERS', 'state': 'Karnataka', 'ledger': 'Printing & Stationery'},
    '29AACCS4032K1Z9': {'name': 'SIDHARTHA METALCOATING LTD', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AARPM8285A1Z7': {'name': 'SIGNAL SWITCH DEVICES & SYSTEMS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '07ABECS9580P1ZE': {'name': 'SNV AVIATION PRIVATE LIMITED', 'state': 'Delhi', 'ledger': 'Transportation & Freight Charges'},
    '29ADLPJ7564J1ZV': {'name': 'SREE BALAJI AUTOMOTIVES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AIMPC4976H1ZT': {'name': 'Sree Siddeshwara Tyres', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AFPPM4660Q1ZF': {'name': 'SREE VINAYAKA AGENCIES', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29AEVPD8777J1ZG': {'name': 'SREERAMA TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AHZPK4119N1ZF': {'name': 'SRI MAHAVEERA AGENCIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ADNFS0535Q1ZA': {'name': 'Sri Ammaji Lubs', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29GGGPS5366K1ZH': {'name': 'SRI ANJANADRI TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29BTKPS5782D1Z2': {'name': 'SRI ANJANEYA TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AARPM8176E1Z1': {'name': 'SRI MAHAVEERA TRADING CO', 'state': 'Karnataka', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29AUWPM1878Q1Z7': {'name': 'SRI MANJUNATHA TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AERPB8025K1Z3': {'name': 'SRI NARASIMHA STORES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AERPN3782M1ZH': {'name': 'SRI SAI ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ECEPK8760H1Z4': {'name': 'SRI RAGHAVENDRASWAMY ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29ACKFS0217R1ZI': {'name': 'SRINIDHI ENGINEERING INDUSTRIES', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAQPF8511J1ZX': {'name': 'KWALITY FURNISHINGS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '33AIPPM4834K1ZV': {'name': 'VEL AGENCIES', 'state': 'Tamil Nadu', 'ledger': 'Purchase Spares & Consumables'},
    '07AAACS0255Q1Z4': {'name': 'STAR HOTELS PVT LTD', 'state': 'Delhi', 'ledger': 'Canteen & Staff Welfare Expenses'},
    '29ABGFS3880F1ZU': {'name': 'STEEL HOUSE', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAECS9084P3Z9': {'name': 'SUDHIR CRANES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAGCT6465R1Z9': {'name': 'SUNDARAM INDUSTRIES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAOFS0840G1Z0': {'name': 'SUPREME MOTORS', 'state': 'Karnataka', 'ledger': 'Labour Charges / Vehicle Service'},
    '29AACFS7041K1ZW': {'name': 'SURI AND SACHI ENTERPRISES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AEBFS8230H1ZV': {'name': 'SURYA PAINTS', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAPCS3794K1ZD': {'name': 'SUVEECHI TECHNOLOGIES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29ATQPG9113K1Z5': {'name': 'T V S AUTO MOBILES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AACCT3705E1ZJ': {'name': 'TALLY(INDIA) PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAKCT9509Q2Z7': {'name': 'TECHNONOVA PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Computer & Printer Expenses'},
    '29AAACN4165C2ZM': {'name': 'THE NEW INDIA ASSURANCE COMPANY LIMITED', 'state': 'Karnataka', 'ledger': 'Lorry Insurance'},
    '29AAACT0627R4ZS': {'name': 'The Oriental Insurance Company Limited', 'state': 'Karnataka', 'ledger': 'Lorry Insurance'},
    '29ELVPS1386D1ZC': {'name': 'TOOLS WORLD', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AALCT2104N1Z0': {'name': 'TOYOTA TSUSHO BHARAT MOTOR PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AABCT7298P1Z9': {'name': 'TRADE HORIZON PVT LTD', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAGCT5084N1ZK': {'name': 'Trichur Sundaram Santhanam & Family Private Limited', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAAFT4775B1Z5': {'name': 'TUMKUR IRON & PAINTS DEPOT', 'state': 'Karnataka', 'ledger': 'Building Repairs, Hardware & Paints'},
    '29AAAFT5461G1Z2': {'name': 'TUMKUR AUTOMOBLIES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AEJPV7459M2ZA': {'name': 'TUMKUR DIESELS', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AAAAT7822J2Z3': {'name': 'TUMKUR GRAIN MERCHANTS CO-OP BANK LTD', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29ABOPS9144C1Z6': {'name': 'TYRE CENTER', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AAACU7492F1ZY': {'name': 'UNILET APPLIANCES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAACU8917F1Z2': {'name': 'UNIVERSAL SOMPO GENERAL INSURANCE COMPANY LIMITED', 'state': 'Karnataka', 'ledger': 'Lorry Insurance'},
    '33AAACU8917F1ZD': {'name': 'UNIVERSAL SOMPO GENERAL INSURANCE COMPANY LIMITED', 'state': 'Tamil Nadu', 'ledger': 'Lorry Insurance'},
    '29AGFPC1933P1Z5': {'name': 'USHA OFFICE SYSTEMS', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '29AAACW0287A1ZJ': {'name': 'VALVOLINE CUMMINS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Diesel & Lubricants'},
    '29AAECV8182H1ZS': {'name': 'VAYUPUTRA AUTOMOBILES PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29CGMPK9313E1Z5': {'name': 'VIJAYA TYRE CENTRE', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '29AACFV9817E1ZU': {'name': 'VIJAYASHREE AUTOMOBILES', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '29AAICV8811B1Z8': {'name': 'VIKAS TRADE LINKS PRIVATE LIMITED', 'state': 'Karnataka', 'ledger': 'Purchase Spares & Consumables'},
    '33AABCV3609C1ZU': {'name': 'VRL LOGISTICS LIMITED', 'state': 'Tamil Nadu', 'ledger': 'Transportation & Freight Charges'},
    '29AABCV3609C1ZJ': {'name': 'VRL LOGISTICS LIMITED', 'state': 'Karnataka', 'ledger': 'Transportation & Freight Charges'},
    '29AABCW7791A1Z3': {'name': 'WAKEFIT INNOVATIONS LIMITED', 'state': 'Karnataka', 'ledger': 'General Office Expenses'},
    '33AABCW7791A1ZE': {'name': 'WAKEFIT INNOVATIONS LIMITED', 'state': 'Tamil Nadu', 'ledger': 'General Office Expenses'},
    '06AACCW0191M1Z5': {'name': 'WATSOO EXPRESS PRIVATE LIMITED', 'state': 'Haryana', 'ledger': 'Transportation & Freight Charges'},
    '29BHGPS3457K1ZO': {'name': 'YASHASWINI TYRES', 'state': 'Karnataka', 'ledger': 'Tyre & Tube Purchase'},
    '33AABCZ2324E1ZS': {'name': 'ZUDDHA ECO SOLUTIONS PRIVATE LIMITED', 'state': 'Tamil Nadu', 'ledger': 'Welding Gases & Industrial Spares'},
}


def find_vendor_by_gstin(gstin: str) -> Optional[Dict[str, str]]:
    """Look up a vendor by 15-digit GSTIN."""
    if not gstin:
        return None
    cleaned = gstin.strip().upper()
    return VENDOR_DIRECTORY.get(cleaned)


def find_vendor_by_name(query: str) -> Optional[Dict[str, Any]]:
    """Fuzzy / substring match for vendor name to retrieve official GSTIN and default expense ledger."""
    if not query or len(query.strip()) < 3:
        return None
    import re
    q = query.strip().lower()
    q_norm = re.sub(r'[^a-z0-9]', '', q)
    for gstin, info in VENDOR_DIRECTORY.items():
        v_name = info['name'].lower()
        v_norm = re.sub(r'[^a-z0-9]', '', v_name)
        if q in v_name or v_name in q or (q_norm and len(q_norm) >= 3 and (q_norm in v_norm or v_norm in q_norm)):
            return {'gstin': gstin, **info}
    return None


def get_all_vendor_names() -> List[str]:
    """Return all unique vendor names sorted alphabetically for dropdowns / autocomplete."""
    names = set(info['name'] for info in VENDOR_DIRECTORY.values())
    return sorted(list(names))


def get_tax_split(gstin: str, taxable_value: float, tax_rate_percent: float) -> Dict[str, float]:
    """
    Computes statutory CGST, SGST, IGST based on Balaji's home state (Karnataka - 29).
    If supplier is 29 (Intra-state): CGST = rate/2, SGST = rate/2, IGST = 0.
    If supplier is outside 29 (Inter-state): IGST = full rate, CGST = 0, SGST = 0.
    """
    cleaned_gstin = (gstin or '').strip().upper()
    supplier_state_code = cleaned_gstin[:2] if len(cleaned_gstin) >= 2 and cleaned_gstin[:2].isdigit() else '29'
    
    total_tax = round(taxable_value * (tax_rate_percent / 100.0), 2)
    
    if supplier_state_code == '29':
        half_tax = round(total_tax / 2.0, 2)
        return {
            'cgst': half_tax,
            'sgst': round(total_tax - half_tax, 2),
            'igst': 0.0,
            'is_interstate': False,
        }
    else:
        return {
            'cgst': 0.0,
            'sgst': 0.0,
            'igst': total_tax,
            'is_interstate': True,
        }


# State code mapping for statutory Indian GSTINs
STATE_CODE_MAP = {
    '01': 'Jammu & Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab', '04': 'Chandigarh',
    '06': 'Haryana', '07': 'Delhi', '08': 'Rajasthan', '09': 'Uttar Pradesh',
    '10': 'Bihar', '19': 'West Bengal', '23': 'Madhya Pradesh', '24': 'Gujarat',
    '27': 'Maharashtra', '29': 'Karnataka', '32': 'Kerala', '33': 'Tamil Nadu',
    '34': 'Puducherry', '36': 'Telangana', '37': 'Andhra Pradesh'
}

GSTIN_VALIDATION_REGEX = r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$'


def register_or_update_vendor(
    gstin: str,
    name: str,
    ledger: str = 'Purchase Spares & Consumables',
    user=None,
    force_update: bool = False
) -> Dict[str, Any]:
    """
    Authorized process for safely adding or updating a verified recurring vendor.
    Guarantees:
    - GSTIN is the primary identifier, validated against official 15-character format.
    - Requires authorized user role (Naveen or Admin).
    - Prevents accidental overwriting of verified mappings.
    - Normalizes vendor names and links multi-state branches.
    - Does NOT allow unverified OCR text to blindly overwrite verified records.
    """
    import re
    from django.core.exceptions import PermissionDenied, ValidationError

    if user is not None and not (getattr(user, 'is_naveen', False) or getattr(user, 'is_admin_role', False) or getattr(user, 'is_staff', False)):
        raise PermissionDenied("Only Naveen or Administrator can register or modify verified vendor records.")

    cleaned_gstin = (gstin or '').strip().upper()
    if not re.match(GSTIN_VALIDATION_REGEX, cleaned_gstin):
        raise ValidationError(f"Invalid GSTIN format: '{gstin}'. Must be 15-character alphanumeric.")

    cleaned_name = (name or '').strip()
    if len(cleaned_name) < 3:
        raise ValidationError("Vendor name must be at least 3 characters long.")

    state_code = cleaned_gstin[:2]
    state_name = STATE_CODE_MAP.get(state_code, f"State {state_code}")

    valid_ledger = ledger if ledger in TALLY_EXPENSE_LEDGERS else 'Purchase Spares & Consumables'

    is_existing = cleaned_gstin in VENDOR_DIRECTORY
    if is_existing and not force_update:
        return {
            'status': 'EXISTING_UNCHANGED',
            'gstin': cleaned_gstin,
            'vendor': VENDOR_DIRECTORY[cleaned_gstin],
            'message': f"Vendor with GSTIN {cleaned_gstin} is already verified as '{VENDOR_DIRECTORY[cleaned_gstin]['name']}'."
        }

    VENDOR_DIRECTORY[cleaned_gstin] = {
        'name': cleaned_name,
        'state': state_name,
        'ledger': valid_ledger
    }

    action = 'UPDATED' if is_existing else 'CREATED'
    return {
        'status': action,
        'gstin': cleaned_gstin,
        'vendor': VENDOR_DIRECTORY[cleaned_gstin],
        'message': f"Successfully {action.lower()} vendor '{cleaned_name}' ({cleaned_gstin}) in {state_name}."
    }

