"""
Contract Generator + Custom E-Signature System.
Generates Texas real estate purchase agreements as PDF.
Custom signature capture (no DocuSign fees).
"""
import io
import os
import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from config.settings import settings

logger = logging.getLogger(__name__)


# ── Texas Purchase Agreement Template ─────────────────────────────

CONTRACT_TEMPLATE_EN = """
REAL ESTATE PURCHASE AND SALE AGREEMENT
(Assignment of Contract)

STATE OF TEXAS
COUNTY OF {county}

Date: {date}

1. PARTIES
   BUYER: {buyer_name} and/or assigns
   Entity: {buyer_entity}
   Address: {buyer_address}
   Phone: {buyer_phone}
   Email: {buyer_email}

   SELLER: {seller_name}
   Address: {seller_address}
   Phone: {seller_phone}
   Email: {seller_email}

2. PROPERTY
   The property located at:
   {property_address}
   {property_city}, {property_state} {property_zip}
   County: {property_county}
   Legal Description / Parcel ID: {parcel_id}

   Property Type: {property_type}
   Approximate Square Footage: {sqft}

3. PURCHASE PRICE AND TERMS
   Purchase Price: ${purchase_price}
   Earnest Money: ${earnest_money} (to be deposited within 3 business days)
   Balance Due at Closing: ${balance_due}

4. EARNEST MONEY
   Earnest money shall be deposited with the title company within three (3)
   business days after the effective date of this agreement.

5. INSPECTION PERIOD
   Buyer shall have {inspection_days} calendar days from the effective date
   to inspect the property. During this period, Buyer may terminate this
   agreement for any reason and receive a full refund of earnest money.

6. CLOSING
   Closing shall occur on or before {closing_date}, unless extended by
   mutual written agreement. Seller shall pay for the owner's title policy.
   Buyer shall pay for all other closing costs.

7. PROPERTY CONDITION
   Seller agrees to sell the property in its current "AS-IS" condition.
   Seller makes no warranties regarding the condition of the property.

8. ASSIGNMENT
   Buyer shall have the right to assign this contract to a third party
   without the consent of the Seller. Any assignment shall not release
   the original Buyer from obligations under this contract unless the
   assignee assumes all obligations in writing.

9. DEFAULT
   If Buyer defaults, Seller's sole remedy shall be to retain the earnest
   money as liquidated damages. If Seller defaults, Buyer may seek specific
   performance or return of earnest money plus actual damages.

10. ADDITIONAL TERMS
    {additional_terms}

11. ENTIRE AGREEMENT
    This agreement constitutes the entire agreement between the parties.
    No modifications shall be binding unless in writing and signed by both parties.

SIGNATURES:

BUYER:
Signature: ___________________________
Name: {buyer_name}
Date: {buyer_sign_date}
{buyer_signature_block}

SELLER:
Signature: ___________________________
Name: {seller_name}
Date: {seller_sign_date}
{seller_signature_block}
"""

CONTRACT_TEMPLATE_ES = """
CONTRATO DE COMPRAVENTA DE BIENES RAÍCES
(Cesión de Contrato)

ESTADO DE TEXAS
CONDADO DE {county}

Fecha: {date}

1. PARTES
   COMPRADOR: {buyer_name} y/o cesionarios
   Entidad: {buyer_entity}
   Dirección: {buyer_address}
   Teléfono: {buyer_phone}
   Correo: {buyer_email}

   VENDEDOR: {seller_name}
   Dirección: {seller_address}
   Teléfono: {seller_phone}
   Correo: {seller_email}

2. PROPIEDAD
   La propiedad ubicada en:
   {property_address}
   {property_city}, {property_state} {property_zip}
   Condado: {property_county}
   Descripción Legal / ID de Parcela: {parcel_id}

   Tipo de Propiedad: {property_type}
   Pies Cuadrados Aproximados: {sqft}

3. PRECIO DE COMPRA Y TÉRMINOS
   Precio de Compra: ${purchase_price}
   Arras: ${earnest_money} (a depositar dentro de 3 días hábiles)
   Saldo a Pagar al Cierre: ${balance_due}

4. ARRAS
   Las arras se depositarán con la compañía de títulos dentro de tres (3)
   días hábiles después de la fecha efectiva de este contrato.

5. PERÍODO DE INSPECCIÓN
   El Comprador tendrá {inspection_days} días calendario desde la fecha
   efectiva para inspeccionar la propiedad. Durante este período, el
   Comprador puede rescindir este contrato por cualquier motivo y recibir
   un reembolso completo de las arras.

6. CIERRE
   El cierre se realizará en o antes del {closing_date}, a menos que se
   extienda por acuerdo mutuo por escrito.

7. CONDICIÓN DE LA PROPIEDAD
   El Vendedor acepta vender la propiedad en su condición actual "TAL COMO ESTÁ".

8. CESIÓN
   El Comprador tendrá el derecho de ceder este contrato a un tercero
   sin el consentimiento del Vendedor.

9. INCUMPLIMIENTO
   Si el Comprador incumple, el único recurso del Vendedor será retener
   las arras como daños liquidados.

10. TÉRMINOS ADICIONALES
    {additional_terms}

11. ACUERDO COMPLETO
    Este contrato constituye el acuerdo completo entre las partes.

FIRMAS:

COMPRADOR:
Firma: ___________________________
Nombre: {buyer_name}
Fecha: {buyer_sign_date}
{buyer_signature_block}

VENDEDOR:
Firma: ___________________________
Nombre: {seller_name}
Fecha: {seller_sign_date}
{seller_signature_block}
"""


class ContractGenerator:
    """Generates purchase agreements and handles e-signatures."""

    def __init__(self):
        self.config = settings.contract
        self.output_dir = os.path.join(os.path.dirname(__file__), "..", "output", "contracts")
        os.makedirs(self.output_dir, exist_ok=True)

    def generate_contract(
        self,
        property_data: dict,
        owner_data: dict,
        deal_data: dict,
        language: str = "en",
    ) -> dict:
        """
        Generate a purchase agreement.

        Returns:
            {
                "contract_text": str,
                "contract_path": str (path to saved file),
                "contract_hash": str (SHA-256 for integrity),
                "metadata": dict,
            }
        """
        template = CONTRACT_TEMPLATE_ES if language == "es" else CONTRACT_TEMPLATE_EN

        now = datetime.now()
        closing_date = now + timedelta(days=self.config.closing_period_days)

        purchase_price = deal_data.get("offered_price") or deal_data.get("max_offer", 0)
        earnest_money = self.config.earnest_money_default
        balance_due = purchase_price - earnest_money

        # Fill template
        contract_text = template.format(
            county=property_data.get("county", ""),
            date=now.strftime("%B %d, %Y"),
            buyer_name=self.config.buyer_name or "Buyer",
            buyer_entity=self.config.buyer_entity or "",
            buyer_address="",
            buyer_phone=self.config.buyer_phone or "",
            buyer_email=self.config.buyer_email or "",
            seller_name=owner_data.get("full_name", "Seller"),
            seller_address=owner_data.get("mailing_address", ""),
            seller_phone=owner_data.get("phone_1", ""),
            seller_email=owner_data.get("email_1", ""),
            property_address=property_data.get("street", ""),
            property_city=property_data.get("city", ""),
            property_state=property_data.get("state", "TX"),
            property_zip=property_data.get("zip_code", ""),
            property_county=property_data.get("county", ""),
            parcel_id=property_data.get("parcel_id", ""),
            property_type=property_data.get("property_type", ""),
            sqft=property_data.get("sqft", ""),
            purchase_price=f"{purchase_price:,.2f}",
            earnest_money=f"{earnest_money:,.2f}",
            balance_due=f"{balance_due:,.2f}",
            inspection_days=self.config.inspection_period_days,
            closing_date=closing_date.strftime("%B %d, %Y"),
            additional_terms="None.",
            buyer_sign_date="",
            seller_sign_date="",
            buyer_signature_block="[ELECTRONIC SIGNATURE PENDING]",
            seller_signature_block="[ELECTRONIC SIGNATURE PENDING]",
        )

        # Generate hash for integrity
        contract_hash = hashlib.sha256(contract_text.encode()).hexdigest()

        # Save contract
        filename = f"contract_{property_data.get('parcel_id', 'unknown')}_{now.strftime('%Y%m%d_%H%M%S')}.txt"
        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, "w") as f:
            f.write(contract_text)

        metadata = {
            "property_address": f"{property_data.get('street', '')} {property_data.get('city', '')}, TX",
            "seller_name": owner_data.get("full_name", ""),
            "purchase_price": purchase_price,
            "generated_at": now.isoformat(),
            "language": language,
        }

        logger.info(f"Contract generated: {filepath}")

        return {
            "contract_text": contract_text,
            "contract_path": filepath,
            "contract_hash": contract_hash,
            "metadata": metadata,
        }

    def apply_signature(
        self,
        contract_path: str,
        signature_data: str,
        signer: str = "buyer",
        signer_name: str = "",
        ip_address: str = "",
    ) -> dict:
        """
        Apply an electronic signature to a contract.

        Args:
            contract_path: Path to the contract file
            signature_data: Base64-encoded signature image from canvas
            signer: "buyer" or "seller"
            signer_name: Name of the signer
            ip_address: IP address for audit trail

        Returns:
            Signature record for audit trail
        """
        now = datetime.now()

        # Read contract
        with open(contract_path, "r") as f:
            contract_text = f.read()

        # Create signature block
        sig_block = (
            f"[ELECTRONICALLY SIGNED]\n"
            f"Signed by: {signer_name}\n"
            f"Date/Time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"IP Address: {ip_address}\n"
            f"Signature Hash: {hashlib.sha256(signature_data.encode()).hexdigest()[:16]}"
        )

        # Replace pending signature block
        if signer == "buyer":
            contract_text = contract_text.replace(
                "[ELECTRONIC SIGNATURE PENDING]", sig_block, 1
            )
        else:
            # Replace the second occurrence (seller)
            parts = contract_text.split("[ELECTRONIC SIGNATURE PENDING]")
            if len(parts) >= 3:
                contract_text = parts[0] + "[ELECTRONIC SIGNATURE PENDING]" + parts[1] + sig_block + "".join(parts[2:])
            elif len(parts) >= 2:
                contract_text = parts[0] + sig_block + parts[1]

        # Save updated contract
        with open(contract_path, "w") as f:
            f.write(contract_text)

        # Create audit record
        audit_record = {
            "contract_path": contract_path,
            "signer": signer,
            "signer_name": signer_name,
            "signed_at": now.isoformat(),
            "ip_address": ip_address,
            "signature_hash": hashlib.sha256(signature_data.encode()).hexdigest(),
            "contract_hash": hashlib.sha256(contract_text.encode()).hexdigest(),
        }

        # Save audit trail
        audit_path = contract_path.replace(".txt", "_audit.json")
        audits = []
        if os.path.exists(audit_path):
            with open(audit_path, "r") as f:
                audits = json.load(f)
        audits.append(audit_record)
        with open(audit_path, "w") as f:
            json.dump(audits, f, indent=2)

        logger.info(f"Signature applied to {contract_path} by {signer_name} ({signer})")
        return audit_record

    def generate_pdf(self, contract_path: str) -> Optional[str]:
        """
        Convert contract text to PDF using reportlab.
        Returns path to PDF file.
        """
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.lib.units import inch

            pdf_path = contract_path.replace(".txt", ".pdf")

            with open(contract_path, "r") as f:
                text = f.read()

            doc = SimpleDocTemplate(pdf_path, pagesize=letter,
                                    topMargin=0.75*inch, bottomMargin=0.75*inch)
            styles = getSampleStyleSheet()

            # Custom style
            body_style = ParagraphStyle(
                "ContractBody",
                parent=styles["Normal"],
                fontSize=10,
                leading=14,
                spaceAfter=6,
            )
            title_style = ParagraphStyle(
                "ContractTitle",
                parent=styles["Heading1"],
                fontSize=14,
                alignment=1,  # Center
                spaceAfter=12,
            )

            elements = []
            for line in text.strip().split("\n"):
                line = line.strip()
                if not line:
                    elements.append(Spacer(1, 6))
                elif line.startswith("REAL ESTATE") or line.startswith("CONTRATO"):
                    elements.append(Paragraph(line, title_style))
                else:
                    # Escape HTML special chars
                    line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    elements.append(Paragraph(line, body_style))

            doc.build(elements)
            logger.info(f"PDF generated: {pdf_path}")
            return pdf_path

        except ImportError:
            logger.warning("reportlab not installed — saving as text only")
            return None
        except Exception as e:
            logger.error(f"PDF generation failed: {e}")
            return None
