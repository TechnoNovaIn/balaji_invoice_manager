from django.urls import path
from . import views

app_name = 'invoices'

urlpatterns = [
    path('upload/', views.upload_invoice, name='upload'),
    path('progress/<int:document_id>/', views.upload_progress, name='upload_progress'),
    path('document-status/<int:document_id>/', views.document_status_api, name='document_status_api'),
    path('success/<str:submission_id>/', views.submission_success, name='submission_success'),
    path('my-submissions/', views.my_submissions, name='my_submissions'),
    path('detail/<str:submission_id>/', views.submission_detail, name='submission_detail'),
    path('download/<str:submission_id>/', views.download_invoice, name='download_invoice'),
    path('batch-download/', views.batch_download_invoices, name='batch_download_invoices'),
    path('resubmit/<str:submission_id>/', views.resubmit_invoice, name='resubmit'),
    path('document/<int:doc_id>/serve/', views.serve_document, name='serve_document'),
    path('document/<int:doc_id>/download/', views.download_document, name='download_document'),
    path('action/<str:submission_id>/return/', views.naveen_return_invoice, name='naveen_return'),
    path('action/<str:submission_id>/reject/', views.naveen_reject_invoice, name='naveen_reject'),
    path('action/<str:submission_id>/verify/', views.naveen_verify_invoice, name='naveen_verify'),
    path('action/<str:submission_id>/split/', views.split_invoice_view, name='split_invoice'),
    path('action/<str:submission_id>/merge/', views.merge_invoices_view, name='merge_invoices'),
    path('source-document/<int:doc_id>/download/', views.download_source_document, name='download_source_document'),
    path('source-document/<int:doc_id>/retry/', views.retry_failed_upload, name='retry_failed_upload'),

    # Tally Accounting & GST Portal Integration
    path('tally/<str:submission_id>/', views.tally_workspace, name='tally_workspace'),
    path('toggle-tally/<str:submission_id>/', views.toggle_tally_status, name='toggle_tally'),
    path('quick-update/<str:submission_id>/', views.quick_update_metadata, name='quick_update_metadata'),
    path('batch-complete/', views.batch_complete_tally, name='batch_complete_tally'),
    path('batch-undo/', views.batch_undo_tally, name='batch_undo_tally'),
    path('toggle-gst/<str:submission_id>/', views.toggle_gst_status, name='toggle_gst'),
    path('export/tally-xml/', views.export_tally_xml, name='export_tally_xml'),
    path('export/gst-csv/', views.export_gst_csv, name='export_gst_csv'),
    path('export/gst-portal-excel/', views.export_gst_portal_excel, name='export_gst_portal_excel'),
    path('gst-converter/', views.gst_converter_tool, name='gst_converter_tool'),

    # Invoice Verification Desk
    path('verify/<str:submission_id>/', views.verification_workspace, name='verification_workspace'),

    # Structured Tally Excel & Accounting Exports
    path('export/tally-excel/', views.export_tally_excel, name='export_tally_excel'),
    path('export-history/', views.export_history_view, name='export_history'),

    # Phase 7: Executive Analytics & Reports
    path('analytics/', views.analytics_dashboard, name='analytics_dashboard'),
    path('reports/<str:report_type>/', views.download_executive_report, name='download_executive_report'),
]

