import unittest
from field_manual_viewer.parallel import needs_acquisition,should_attempt_format

class QueueTests(unittest.TestCase):
    def test_completed_sample_is_not_downloaded_again(self):
        good={('book','pdf'):'pdf-sha',('book','epub'):'epub-sha'}
        self.assertFalse(needs_acquisition('book',good,set(),False))
        self.assertTrue(needs_acquisition('new',good,set(),False))

    def test_failed_pdf_does_not_trigger_automatic_retry(self):
        good={('book','epub'):'epub-sha'}
        failed={('book','pdf')}
        self.assertFalse(needs_acquisition('book',good,failed,False))
        self.assertFalse(should_attempt_format('book','pdf',good,failed,False))
        self.assertTrue(needs_acquisition('book',good,failed,True))

if __name__=='__main__':unittest.main()
