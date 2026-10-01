"""The media bucket template (infra/media.yaml): customer media is kept
permanently (decision 2026-09-29, spec docs/superpowers/specs/2026-09-29-
customer-media-in-s3-design.md §4). No lifecycle rule may expire a current
object under `customers/`; superseded versions still expire at 30 days and
abandoned multipart uploads at 2, and versioning stays on.
"""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "infra" / "media.yaml"


class _TolerantLoader(yaml.SafeLoader):
    """A SafeLoader that accepts CloudFormation's short intrinsic-function
    tags (!Sub, !Ref, !GetAtt, ...) instead of raising on them. Each tag's
    value is returned as a plain marker (the scalar, or a dict/list for the
    mapping/sequence forms) - good enough to inspect the template's shape
    without caring what any given intrinsic function resolves to.
    """


def _construct_any(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


_TolerantLoader.add_multi_constructor("!", _construct_any)


def load_template():
    with TEMPLATE.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_TolerantLoader)


def _applies_to_customers(rule):
    if rule.get("Prefix") == "customers/":
        return True
    filter_ = rule.get("Filter")
    if isinstance(filter_, dict):
        if filter_.get("Prefix") == "customers/":
            return True
        and_ = filter_.get("And")
        if isinstance(and_, dict) and and_.get("Prefix") == "customers/":
            return True
    return False


def _lifecycle_rules():
    template = load_template()
    bucket = template["Resources"]["MediaBucket"]["Properties"]
    return bucket, bucket["LifecycleConfiguration"]["Rules"]


class MediaTemplateRetentionTests(unittest.TestCase):
    def test_the_template_parses_with_cloudformation_tags(self):
        template = load_template()
        self.assertIn("MediaBucket", template["Resources"])

    def test_no_customer_rule_expires_current_objects(self):
        _, rules = _lifecycle_rules()
        customer_rules = [rule for rule in rules if _applies_to_customers(rule)]
        self.assertTrue(customer_rules, "expected at least one lifecycle rule for customers/")
        for rule in customer_rules:
            self.assertNotIn(
                "ExpirationInDays", rule,
                "rule %r expires current customers/ objects" % rule.get("Id"),
            )
            self.assertNotIn(
                "ExpirationDate", rule,
                "rule %r expires current customers/ objects" % rule.get("Id"),
            )

    def test_a_customer_rule_still_expires_noncurrent_versions_at_30_days(self):
        _, rules = _lifecycle_rules()
        customer_rules = [rule for rule in rules if _applies_to_customers(rule)]
        noncurrent_days = [
            rule["NoncurrentVersionExpiration"]["NoncurrentDays"]
            for rule in customer_rules
            if "NoncurrentVersionExpiration" in rule
        ]
        self.assertIn(30, noncurrent_days)

    def test_the_abandoned_multipart_rule_is_kept_at_2_days(self):
        _, rules = _lifecycle_rules()
        multipart_rules = [rule for rule in rules if "AbortIncompleteMultipartUpload" in rule]
        self.assertTrue(multipart_rules, "expected the abandoned-multipart rule to still exist")
        self.assertTrue(
            any(
                rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 2
                for rule in multipart_rules
            )
        )

    def test_the_server_may_only_plainly_delete_customer_files(self):
        statements = load_template()["Resources"]["MediaAccessPolicy"]["Properties"]["PolicyDocument"]["Statement"]
        deletes = [s for s in statements if "s3:DeleteObject" in (s["Action"] if isinstance(s["Action"], list)
                                                                  else [s["Action"]])]
        self.assertEqual(len(deletes), 1)
        self.assertIn("/customers/*", str(deletes[0]["Resource"]))
        for statement in statements:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            self.assertNotIn("s3:DeleteObjectVersion", actions)

    def test_delete_markers_left_after_30_days_are_removed(self):
        _, rules = _lifecycle_rules()
        self.assertTrue(any(_applies_to_customers(rule) and rule.get("ExpiredObjectDeleteMarker") is True
                            for rule in rules))

    def test_versioning_stays_enabled(self):
        bucket, _ = _lifecycle_rules()
        self.assertEqual(bucket["VersioningConfiguration"]["Status"], "Enabled")


if __name__ == "__main__":
    unittest.main()
