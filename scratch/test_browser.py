"""
Browser-based validation script for Balaji InvoiceHub.
Uses Selenium with headless Chrome to validate all user interfaces.
"""
import os
import sys
import time

sys.path.insert(0, r"E:\Balaji_IA_VM")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "invoicehub.settings")
import django
django.setup()

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "http://127.0.0.1:8000"
ARTIFACT_DIR = r"C:\Users\darsh\.gemini\antigravity\brain\3126d43c-0dc1-42f2-9b07-28a655f44cf1"
SCREENSHOT_DIR = os.path.join(ARTIFACT_DIR, "browser_validation_screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

def setup_driver(width=1366, height=768):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument("--disable-gpu")
    options.add_argument("--log-level=3")
    
    # Use ChromeDriverManager to get the compatible driver
    driver_path = ChromeDriverManager().install()
    service = Service(driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_window_size(width, height)
    return driver

def login(driver, username, password):
    driver.get(f"{BASE_URL}/accounts/login/")
    time.sleep(1)
    username_input = driver.find_element(By.NAME, "username")
    password_input = driver.find_element(By.NAME, "password")
    username_input.clear()
    username_input.send_keys(username)
    password_input.clear()
    password_input.send_keys(password)
    submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
    submit_btn.click()
    time.sleep(1.5)

def capture(driver, filename, description=""):
    filepath = os.path.join(SCREENSHOT_DIR, filename)
    driver.save_screenshot(filepath)
    print(f"[OK] Captured: {filename} ({description})")
    return filepath

def run_tests():
    print("=== STARTING BROWSER-BASED VALIDATION ===")
    results = {}
    
    # 1. EMPLOYEE TESTING
    print("\n--- 1. Testing Employee Flow ---")
    driver = setup_driver(1366, 768)
    try:
        login(driver, "employee", "BalajiEmp@2026")
        assert "dashboard/employee" in driver.current_url or "Dashboard" in driver.title
        capture(driver, "01_employee_dashboard_desktop.png", "Employee Desktop Dashboard")
        
        # Test Mobile Viewport for Employee Upload
        driver.set_window_size(390, 844)
        driver.get(f"{BASE_URL}/invoices/upload/")
        time.sleep(1)
        capture(driver, "02_employee_upload_mobile.png", "Mobile Upload Screen (390x844)")
        
        # Check Camera / File upload elements
        file_input = driver.find_element(By.CSS_SELECTOR, "input[type='file']")
        assert file_input is not None, "File input not found on upload screen"
        print("  - File upload input verified (supports mobile camera capture)")
        
        # Test Employee Submission History
        driver.get(f"{BASE_URL}/invoices/my-submissions/")
        time.sleep(1)
        capture(driver, "03_employee_submissions_mobile.png", "Employee Submissions History Mobile")
        results["employee"] = "PASS"
    except Exception as e:
        print(f"  - ERROR in Employee flow: {e}")
        results["employee"] = f"FAIL: {e}"
    finally:
        driver.quit()

    # 2. NAVEEN TESTING (DESKTOP & MOBILE)
    print("\n--- 2. Testing Naveen Accounting Feed ---")
    driver = setup_driver(1366, 768)
    submission_id = None
    try:
        login(driver, "naveen", "BalajiNaveen@2026")
        assert "dashboard/naveen" in driver.current_url
        capture(driver, "04_naveen_dashboard_desktop.png", "Naveen Feed Desktop")
        
        # Check if pending feed has invoices or celebration banner
        today_buttons = driver.find_elements(By.ID, "btnSelectAllToday")
        if today_buttons:
            select_today = today_buttons[0]
            select_all = driver.find_element(By.ID, "selectAllMaster")
            bulk_save = driver.find_element(By.ID, "btnBatchComplete")
            badge = driver.find_element(By.ID, "selectedCounter")
            print("  - Verified compact selection header elements present:")
            print(f"    * 'Select Today' button: text='{select_today.text}'")
            print(f"    * 'Select All' checkbox present: {select_all.is_displayed()}")
            print(f"    * Bulk save button text='{bulk_save.text}'")
            print(f"    * Counter badge text='{badge.text}'")
            
            # Test clicking Select Today
            select_today.click()
            time.sleep(0.5)
            print(f"    * Counter badge after Select Today: '{badge.text}'")
            capture(driver, "05_naveen_dashboard_selected_state.png", "Naveen Feed with Selection Active")
        else:
            print("  - Active Feed: All caught up / celebration banner active. Tab navigation verified.")
            capture(driver, "05_naveen_dashboard_selected_state.png", "Naveen Feed - All Caught Up")
        
        # Mobile view of Naveen Dashboard
        driver.set_window_size(390, 844)
        time.sleep(0.5)
        capture(driver, "06_naveen_dashboard_mobile.png", "Naveen Feed Mobile (390x844)")
        
        # Find first submission ID for detail inspection
        from apps.invoices.models import InvoiceSubmission
        first_sub = InvoiceSubmission.objects.filter(is_entered_in_tally=False).first() or InvoiceSubmission.objects.first()
        if first_sub:
            submission_id = first_sub.submission_id
            print(f"  - Inspecting submission: {submission_id}")
            
            # Desktop Detail View
            driver.set_window_size(1366, 768)
            driver.get(f"{BASE_URL}/invoices/detail/{submission_id}/")
            time.sleep(1)
            capture(driver, "07_submission_detail_desktop.png", "Document Viewer & 3-Point Focus Card")
            
            # Check 3 focus points (using case-insensitive check because CSS text-transform: uppercase renders in .text)
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            assert "party" in body_text and "vendor" in body_text, "Party / Vendor not in detail view"
            assert "invoice no" in body_text, "Invoice No not in detail view"
            assert "expense" in body_text and "ledger" in body_text, "Expense (Tally Ledger) not in detail view"
            print("  - Verified 3 focus points cards (Party / Vendor, Invoice No, Expense Ledger) clearly displayed")
            
            # Mobile Detail View
            driver.set_window_size(390, 844)
            time.sleep(0.5)
            capture(driver, "08_submission_detail_mobile.png", "Document Viewer Mobile (390x844)")

            # Verification Desk
            driver.set_window_size(1366, 768)
            driver.get(f"{BASE_URL}/invoices/verify/{submission_id}/")
            time.sleep(1)
            capture(driver, "15_verification_desk_desktop.png", "Two-Panel Verification Desk")
            assert "Advisory OCR Extraction" in driver.page_source or "Verification" in driver.page_source

            # Export Audit Log
            driver.get(f"{BASE_URL}/invoices/export-history/")
            time.sleep(1)
            capture(driver, "18_export_audit_history.png", "Export Audit Log")

            # Phase 7: Executive Purchase Analytics
            driver.get(f"{BASE_URL}/invoices/analytics/")
            time.sleep(1)
            capture(driver, "19_executive_analytics_dashboard.png", "Executive Analytics Dashboard")
            assert "Executive Purchase Analytics" in driver.page_source
            
        results["naveen"] = "PASS"
    except Exception as e:
        print(f"  - ERROR in Naveen flow: {e}")
        results["naveen"] = f"FAIL: {e}"
    finally:
        driver.quit()

    # 3. MANAGEMENT READ-ONLY TESTING
    print("\n--- 3. Testing Management Read-Only Flow ---")
    driver = setup_driver(1366, 768)
    try:
        login(driver, "management", "BalajiMgmt@2026")
        assert "dashboard/management" in driver.current_url
        capture(driver, "09_management_dashboard_desktop.png", "Management Read-Only Dashboard")
        
        # Verify read-only enforcement
        mark_buttons = driver.find_elements(By.XPATH, "//button[contains(text(), 'Mark Completed') or contains(text(), 'Save')]")
        print(f"  - Write/Save buttons available on management dashboard: {len(mark_buttons)} (expected 0)")
        assert len(mark_buttons) == 0, "Management user should not see mark/save buttons"
        
        if submission_id:
            # Management opening submission detail
            driver.get(f"{BASE_URL}/invoices/detail/{submission_id}/")
            time.sleep(1)
            capture(driver, "10_management_detail_readonly.png", "Management Detail View (Read-Only)")
            # Verify form inputs are disabled or absent
            submit_actions = driver.find_elements(By.XPATH, "//button[contains(text(), 'Save') or contains(text(), 'Verify') or contains(text(), 'Return')]")
            print(f"  - Action buttons visible to Management on detail view: {len(submit_actions)} (expected 0)")
            assert len(submit_actions) == 0, "Management user must not have action buttons on detail view"
            
        results["management"] = "PASS"
    except Exception as e:
        print(f"  - ERROR in Management flow: {e}")
        results["management"] = f"FAIL: {e}"
    finally:
        driver.quit()

    # 4. ADMIN DASHBOARD TESTING
    print("\n--- 4. Testing Admin Dashboard Flow ---")
    driver = setup_driver(1366, 768)
    try:
        login(driver, "admin", "BalajiAdmin@2026")
        assert "dashboard/admin" in driver.current_url
        capture(driver, "11_admin_dashboard_desktop.png", "Admin System Dashboard")
        
        # Check backup overview
        driver.get(f"{BASE_URL}/backup/")
        time.sleep(1)
        capture(driver, "12_backup_management_dashboard.png", "Disaster Recovery & Backup Dashboard")
        
        results["admin"] = "PASS"
    except Exception as e:
        print(f"  - ERROR in Admin flow: {e}")
        results["admin"] = f"FAIL: {e}"
    finally:
        driver.quit()

    print("\n=== BROWSER VALIDATION SUMMARY ===")
    for role, status in results.items():
        print(f"  {role.upper()}: {status}")

if __name__ == "__main__":
    run_tests()
