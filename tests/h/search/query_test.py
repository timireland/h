# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import datetime
import elasticsearch_dsl
import mock
import pytest
import webob

from h.search import Search, index, query
from hypothesis import strategies as st
from hypothesis import given

from h.services.annotation_moderation import AnnotationModerationService

MISSING = object()
ES_VERSION = (1, 7, 0)
OFFSET_DEFAULT = 0
LIMIT_DEFAULT = query.LIMIT_DEFAULT
LIMIT_MAX = query.LIMIT_MAX
OFFSET_MAX = query.OFFSET_MAX


@pytest.mark.usefixtures('moderation_service')
class TestLimiter(object):
    def test_it_limits_number_of_annotations(self, Annotation, search):
        ann_ids = [Annotation().id,
                   Annotation().id,
                   Annotation().id,
                   Annotation().id]

        params = webob.multidict.MultiDict([("offset", 1),
                            ("limit", 2)])
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(ann_ids[1:3])

    @pytest.mark.parametrize('offset,from_', [
        # defaults to OFFSET_DEFAULT
        (MISSING, OFFSET_DEFAULT),
        # straightforward pass-through
        (7, 7),
        (42, 42),
        # string values should be converted
        ("23", 23),
        ("82", 82),
        # invalid values should be ignored and the default should be returned
        ("foo",  OFFSET_DEFAULT),
        ("",     OFFSET_DEFAULT),
        ("   ",  OFFSET_DEFAULT),
        ("-23",  OFFSET_DEFAULT),
        ("32.7", OFFSET_DEFAULT),
        ("9801", OFFSET_MAX),
    ])
    def test_offset(self, es_dsl_search, pyramid_request, offset, from_):
        limiter = query.Limiter()

        params = {"offset": offset}
        if offset is MISSING:
            params = {}

        q = limiter(es_dsl_search, params).to_dict()

        assert q["from"] == from_

    @given(st.text())
    @pytest.mark.fuzz
    def test_limit_output_within_bounds(self, es_dsl_search, pyramid_request, text):
        """Given any string input, output should be in the allowed range."""
        limiter = query.Limiter()

        q = limiter(es_dsl_search, {"limit": text}).to_dict()

        assert isinstance(q["size"], int)
        assert 0 <= q["size"] <= LIMIT_MAX

    @given(st.integers())
    @pytest.mark.fuzz
    def test_limit_output_within_bounds_int_input(self, es_dsl_search, pyramid_request, lim):
        """Given any integer input, output should be in the allowed range."""
        limiter = query.Limiter()

        q = limiter(es_dsl_search, {"limit": str(lim)}).to_dict()

        assert isinstance(q["size"], int)
        assert 0 <= q["size"] <= LIMIT_MAX

    @given(st.integers(min_value=0, max_value=LIMIT_MAX))
    @pytest.mark.fuzz
    def test_limit_matches_input(self, es_dsl_search, pyramid_request, lim):
        """Given an integer in the allowed range, it should be passed through."""
        limiter = query.Limiter()

        q = limiter(es_dsl_search, {"limit": str(lim)}).to_dict()

        assert q["size"] == lim

    def test_limit_set_to_default_when_missing(self, es_dsl_search, pyramid_request):
        limiter = query.Limiter()

        q = limiter(es_dsl_search, {}).to_dict()

        assert q["size"] == LIMIT_DEFAULT

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.Limiter())
        return search


@pytest.mark.usefixtures('moderation_service')
class TestKeyValueMatcher(object):
    def test_ands_multiple_key_values(self, Annotation, search):
        ann_ids = [Annotation().id,
                   Annotation().id]
        reply1 = Annotation(references=[ann_ids[0]]).id
        reply2 = Annotation(references=[ann_ids[0], reply1]).id

        params = webob.multidict.MultiDict([("references", ann_ids[0]),
                            ("references", reply1)])
        result = search.run(params)

        assert result.annotation_ids == [reply2]

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.KeyValueMatcher())
        return search


@pytest.mark.usefixtures('moderation_service')
class TestSorter(object):
    @pytest.mark.parametrize("sort_key,order,expected_order", [
        # Sort supports "updated" and "created" fields.
        ("updated", "desc", [1, 0, 2]),
        ("updated", "asc", [2, 0, 1]),
        ("created", "desc", [2, 0, 1]),
        ("created", "asc", [1, 0, 2]),
        ("group", "asc", [2, 0, 1]),
        ("id", "asc", [0, 2, 1]),
        ("user", "asc", [2, 0, 1]),

        # Default sort order should be descending.
        ("updated", None, [1, 0, 2]),

        # Default sort field should be "updated".
        (None, "asc", [2, 0, 1]),
    ])
    def test_it_sorts_annotations(self, Annotation, search, sort_key, order, expected_order):
        dt = datetime.datetime

        # nb. Test annotations have a different ordering for updated vs created
        # and creation order is different than updated/created asc/desc.
        ann_ids = [Annotation(
                    updated=dt(2017, 1, 1),
                    groupid="12345",
                    userid="acct:foo@auth1",
                    id="1",
                    created=dt(2017, 1, 1)).id,
                   Annotation(
                    updated=dt(2018, 1, 1),
                    groupid="12347",
                    userid="acct:foo@auth2",
                    id="9",
                    created=dt(2016, 1, 1)).id,
                   Annotation(
                    updated=dt(2016, 1, 1),
                    groupid="12342",
                    userid="acct:boo@auth1",
                    id="2",
                    created=dt(2018, 1, 1)).id]

        params = {}
        if sort_key:
            params["sort"] = sort_key
        if order:
            params["order"] = order
        result = search.run(params)

        actual_order = [ann_ids.index(id_) for id_ in result.annotation_ids]
        assert actual_order == expected_order

    def test_incomplete_date_defaults_to_min_datetime_values(self, es_dsl_search, pyramid_request):
        """
        The default date should be:
            1970, 1st month, 1st day, 0 hrs, 0 min, 0 sec, 0 ms
        """
        sorter = query.Sorter()

        params = {"search_after": "2018"}

        q = sorter(es_dsl_search, params).to_dict()

        assert q["search_after"] == [1514764800000.0]

    def test_it_ignores_unknown_sort_fields(self, search):
        search.run({"sort": "no_such_field"})

    @pytest.mark.parametrize("date,expected",
        [("1514773561300", [2]),
         ("2018-01-01T02:26:01.03", [2]),
         ("2018-01-01T02:26:01.03+00:00", [2]),
         ("2018-01-01T02:26:01.037224+00:00", [2]),
         ("2017-01", [1, 2]),
         ("2017", [1, 2]),
         ("2018-01-01", [1, 2])],
    )
    def test_it_finds_all_annotations_after_date(
        self, search, Annotation, date, expected,
    ):
        dt = datetime.datetime

        ann_ids = [Annotation(updated=dt(2017, 1, 1), created=dt(2017, 1, 1)).id,
                   Annotation(updated=dt(2018, 1, 1, 2, 26, 1), created=dt(2016, 1, 1)).id,
                   Annotation(updated=dt(2018, 1, 1, 2, 26, 1, 500000), created=dt(2016, 1, 1)).id,
                   Annotation(updated=dt(2016, 1, 1), created=dt(2018, 1, 1)).id]

        result = search.run({"search_after": date, "order": "asc"})

        assert sorted(result.annotation_ids) == sorted([ann_ids[idx] for idx in expected])

    def test_it_finds_all_annotations_after_id(self, search, Annotation):
        ann_ids = sorted([str(Annotation(id="09").id),
                          str(Annotation(id="11").id),
                          str(Annotation(id="02").id)])

        result = search.run({"search_after": ann_ids[1], "sort": "id", "order": "asc"})

        assert result.annotation_ids == [ann_ids[2]]

    def test_it_ignores_search_after_if_invalid_date_format(self, search, Annotation):
        dt = datetime.datetime

        ann_ids = [Annotation(updated=dt(2016, 1, 1), created=dt(2018, 1, 1)).id,
                   Annotation(updated=dt(2017, 1, 1), created=dt(2017, 1, 1)).id,
                   Annotation(updated=dt(2018, 1, 1, 2, 26, 1), created=dt(2016, 1, 1)).id]

        result = search.run({"search_after": "invalid_date", "order": "asc"})

        assert result.annotation_ids == ann_ids


@pytest.mark.usefixtures('moderation_service')
class TestTopLevelAnnotationsFilter(object):

    def test_it_filters_out_replies_but_leaves_annotations_in(self, Annotation, search):
        annotation = Annotation()
        Annotation(references=[annotation.id])

        result = search.run({})

        assert [annotation.id] == result.annotation_ids

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.TopLevelAnnotationsFilter())
        return search


@pytest.mark.usefixtures('moderation_service')
class TestAuthorityFilter(object):
    def test_it_filters_out_non_matching_authorities(self, Annotation, search):
        annotations_auth1 = [Annotation(userid="acct:foo@auth1").id,
                             Annotation(userid="acct:bar@auth1").id]
        # Make some other annotations that are of different authority.
        Annotation(userid="acct:bat@auth2")
        Annotation(userid="acct:bar@auth3")

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(annotations_auth1)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.AuthorityFilter("auth1"))
        return search


@pytest.mark.usefixtures('moderation_service')
class TestAuthFilter(object):
    def test_logged_out_user_can_not_see_private_annotations(self, search, Annotation):
        Annotation()
        Annotation()

        result = search.run({})

        assert not result.annotation_ids

    def test_logged_out_user_can_see_shared_annotations(self, search, Annotation):
        shared_ids = [Annotation(shared=True).id,
                      Annotation(shared=True).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(shared_ids)

    def test_logged_in_user_can_only_see_their_private_annotations(self, search, pyramid_config,
                                                                   Annotation):
        userid = "acct:bar@auth2"
        pyramid_config.testing_securitypolicy(userid)
        # Make a private annotation from a different user.
        Annotation(userid="acct:foo@auth2").id
        users_private_ids = [Annotation(userid=userid).id,
                             Annotation(userid=userid).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(users_private_ids)

    def test_logged_in_user_can_see_shared_annotations(self, search, pyramid_config, Annotation):
        userid = "acct:bar@auth2"
        pyramid_config.testing_securitypolicy(userid)
        shared_ids = [Annotation(userid="acct:foo@auth2", shared=True).id,
                      Annotation(userid=userid, shared=True).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(shared_ids)

    @pytest.fixture
    def search(self, search, pyramid_request):
        search.append_modifier(query.AuthFilter(pyramid_request))
        return search


@pytest.mark.usefixtures('moderation_service')
class TestGroupFilter(object):
    def test_matches_only_annotations_from_specified_group(self, search, Annotation, group):
        Annotation(groupid='group2')
        Annotation(groupid='group3')
        group1_annotations = [Annotation(groupid=group.pubid).id,
                              Annotation(groupid=group.pubid).id]

        result = search.run({'group': group.pubid})

        assert sorted(result.annotation_ids) == sorted(group1_annotations)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.GroupFilter())
        return search

    @pytest.fixture
    def group(self, factories):
        return factories.OpenGroup(name="group1", pubid="group1id")


@pytest.mark.usefixtures('moderation_service')
class TestGroupAuthFilter(object):
    def test_does_not_return_annotations_if_group_not_readable_by_user(
        self, search, Annotation, group_service,
    ):
        group_service.groupids_readable_by.return_value = []
        Annotation(groupid="group2").id
        Annotation(groupid="group1").id
        Annotation(groupid="group1").id

        result = search.run({})

        assert not result.annotation_ids

    def test_returns_annotations_if_group_readable_by_user(
        self, search, Annotation, group_service,
    ):
        group_service.groupids_readable_by.return_value = ["group1"]
        Annotation(groupid="group2", shared=True).id
        expected_ids = [Annotation(groupid="group1").id,
                        Annotation(groupid="group1").id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    @pytest.fixture
    def search(self, search, pyramid_request):
        search.append_modifier(query.GroupAuthFilter(pyramid_request))
        return search


@pytest.mark.usefixtures('moderation_service')
class TestUserFilter(object):
    def test_filters_annotations_by_user(self, search, Annotation):
        Annotation(userid="acct:foo@auth2", shared=True)
        expected_ids = [Annotation(userid="acct:bar@auth2", shared=True).id]

        result = search.run({'user': "bar"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_filter_is_case_insensitive(self, search, Annotation):
        ann_id = Annotation(userid="acct:bob@example", shared=True).id

        result = search.run({"user": "BOB"})

        assert result.annotation_ids == [ann_id]

    def test_filters_annotations_by_multiple_users(self, search, Annotation):
        Annotation(userid="acct:foo@auth2", shared=True)
        expected_ids = [Annotation(userid="acct:bar@auth2", shared=True).id,
                        Annotation(userid="acct:baz@auth2", shared=True).id]

        params = webob.multidict.MultiDict()
        params.add("user", "bar")
        params.add("user", "baz")
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_filters_annotations_by_user_and_authority(self, search, Annotation):
        Annotation(userid="acct:foo@auth2", shared=True)
        expected_ids = [Annotation(userid="acct:foo@auth3", shared=True).id]

        result = search.run({"user": "foo@auth3"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.UserFilter())
        return search


@pytest.mark.usefixtures('moderation_service')
class TestUriFilter(object):
    @pytest.mark.parametrize("field", ("uri", "url"))
    def test_filters_by_field(self, search, Annotation, field):
        Annotation(target_uri="https://foo.com")
        expected_ids = [Annotation(target_uri="https://bar.com").id]

        result = search.run({field: "https://bar.com"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_filters_on_whole_url(self, search, Annotation):
        Annotation(target_uri="http://bar.com/foo")
        expected_ids = [Annotation(target_uri="http://bar.com").id,
                        Annotation(target_uri="http://bar.com/").id]

        result = search.run({"url": "http://bar.com"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_filter_matches_invalid_uri(self, search, Annotation):
        Annotation(target_uri="https://bar.com")
        expected_ids = [Annotation(target_uri="invalid-uri").id]

        result = search.run({"uri": "invalid-uri"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_filters_aliases_http_and_https(self, search, Annotation):
        expected_ids = [Annotation(target_uri="http://bar.com").id,
                        Annotation(target_uri="https://bar.com").id]

        result = search.run({"url": "http://bar.com"})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_returns_all_annotations_with_equivalent_uris(self, search, Annotation, storage):
        # Mark all these uri's as equivalent uri's.
        storage.expand_uri.side_effect = lambda _, x: [
            "urn:x-pdf:1234",
            "file:///Users/june/article.pdf",
            "doi:10.1.1/1234",
            "http://reading.com/x-pdf",
        ]
        Annotation(target_uri="urn:x-pdf:1235")
        Annotation(target_uri="file:///Users/jane/article.pdf").id
        expected_ids = [Annotation(target_uri="urn:x-pdf:1234").id,
                        Annotation(target_uri="doi:10.1.1/1234").id,
                        Annotation(target_uri="http://reading.com/x-pdf").id,
                        Annotation(target_uri="file:///Users/june/article.pdf").id]

        params = webob.multidict.MultiDict()
        params.add("url", "urn:x-pdf:1234")
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_ors_multiple_url_uris(self, search, Annotation):
        Annotation(target_uri="http://baz.com")
        Annotation(target_uri="https://www.foo.com")
        expected_ids = [Annotation(target_uri="https://bar.com").id,
                        Annotation(target_uri="http://bat.com").id,
                        Annotation(target_uri="http://foo.com").id,
                        Annotation(target_uri="https://foo.com/bar").id]

        params = webob.multidict.MultiDict()
        params.add("uri", "http://bat.com")
        params.add("uri", "https://bar.com")
        params.add("url", "http://foo.com")
        params.add("url", "https://foo.com/bar")
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    # TODO - Explicit test of URL normalization (ie. that search normalizes input
    # URL using `h.util.uri.normalize` and queries with that).

    @pytest.fixture
    def search(self, search, pyramid_request):
        search.append_modifier(query.UriFilter(pyramid_request))
        return search

    @pytest.fixture
    def storage(self, patch):
        return patch('h.search.query.storage')


@pytest.mark.usefixtures('moderation_service')
class TestUriCombinedWildcardFilter():

    @pytest.mark.parametrize('params,expected_ann_indexes,separate_keys', [

    # Test with separate_keys = True (aka uri/url are exact match & wildcard_uri is wildcard match.)
    (webob.multidict.MultiDict([("wildcard_uri", "http://bar.com/baz?45")]),
     [2, 3],
     True),
    (webob.multidict.MultiDict([("uri", "urn:x-pdf:a34480f5dbed8c4482a3a921e0196d2a"),
                                ("wildcard_uri", "http://bar.com/baz*45")]),
     [2, 3, 4, 5],
     True),
    (webob.multidict.MultiDict([("uri", "urn:x-pdf:a34480f5dbed8c4482a3a921e0196d2a"),
                                ("url", "http://bar.com/baz*45")]),
     [3, 5],
     True),

    # Test with separate_keys = False (aka uri/url contain both exact &  wildcard matches.)
    (webob.multidict.MultiDict([("uri", "http://bar.com/baz-45?")]),
     [1],
     False),
    (webob.multidict.MultiDict([("uri", "http://bar.com/*")]),
     [0, 1, 2, 3, 4],
     False),
    (webob.multidict.MultiDict([("uri", "urn:x-pdf:a34480f5dbed8c4482a3a921e0196d2a"),
                                ("uri", "http://bar.com/baz*45")]),
     [2, 3, 4, 5],
     False),
    ])
    def test_matches(
        self,
        search,
        pyramid_request,
        Annotation,
        params,
        expected_ann_indexes,
        separate_keys,
    ):
        """
        All uri matches (wildcard and exact) are OR'd.
        """
        search = self._get_search(search, pyramid_request, separate_keys)

        ann_ids = [Annotation(target_uri="http://bar.com?foo").id,
                   Annotation(target_uri="http://bar.com/baz-457").id,
                   Annotation(target_uri="http://bar.com/baz-45").id,
                   Annotation(target_uri="http://bar.com/baz*45").id,
                   Annotation(target_uri="http://bar.com/baz/*/45").id,
                   Annotation(target_uri="urn:x-pdf:a34480f5dbed8c4482a3a921e0196d2a").id]

        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted([ann_ids[ann] for ann in expected_ann_indexes])

    @pytest.mark.parametrize('params,separate_keys', [
        (webob.multidict.MultiDict([("wildcard_uri", "http?://bar.com")]), True),
        (webob.multidict.MultiDict([("url", "ur*n:x-pdf:*")]), False),
    ])
    def test_ignores_urls_with_wildcards_in_the_domain(self, es_dsl_search, pyramid_request, params, separate_keys):
        urifilter = query.UriCombinedWildcardFilter(pyramid_request, separate_keys)

        q = urifilter(es_dsl_search, params).to_dict()

        assert "should" not in q['query']['bool']

    @pytest.mark.parametrize('params,separate_keys', [
        (webob.multidict.MultiDict([("wildcard_uri", "http?://bar.com"),
                                    ("uri", "http://bar.com"),
                                    ("url", "http://baz.com")]), True),
        (webob.multidict.MultiDict([("uri", "http?://bar.com"),
                                    ("url", "http://baz.com")]), False),
    ])
    def test_pops_params(self, es_dsl_search, pyramid_request, params, separate_keys):
        urifilter = query.UriCombinedWildcardFilter(pyramid_request, separate_keys)

        urifilter(es_dsl_search, params).to_dict()

        assert "uri" not in params
        assert "url" not in params
        assert "wildcard_uri" not in params

    def _get_search(self, search, pyramid_request, separate_keys):
        search.append_modifier(query.UriCombinedWildcardFilter(
            pyramid_request, separate_keys))
        return search


@pytest.mark.parametrize('wildcard_uri,expected', [
    ("http?://bar.com", False),
    ("htt*://bar.com", False),
    ("http://localhost:3000*", False),
    ("http://bar*.com", False),
    ("http://bar?com", False),
    ("*?http://bar.com", False),
    ("file://*", False),
    ("https://foo.com", False),
    ("http://foo.com*", False),
    ("urn:*", True),
    ("urn:x-pdf:*", True),
    ("http://foo.com/*", True),
    ("doi:10.101?", True),
    ("http://*.org/*", False),
    ("http://example.*", False),
])
def test_identifies_wildcard_uri_is_valid(wildcard_uri, expected):
    assert query.wildcard_uri_is_valid(wildcard_uri) == expected


@pytest.mark.usefixtures('moderation_service')
class TestDeletedFilter(object):

    def test_excludes_deleted_annotations(self, search, es_client, Annotation):
        deleted_ids = [Annotation(deleted=True).id]
        not_deleted_ids = [Annotation(deleted=False).id]

        # Deleted annotations need to be marked in the index using `h.search.index.delete`.
        for id_ in deleted_ids:
            index.delete(es_client, id_, refresh=True)

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(not_deleted_ids)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.DeletedFilter())
        return search


@pytest.mark.usefixtures('pyramid_config', 'moderation_service')
class TestHiddenFilter(object):

    @pytest.mark.parametrize('nipsa_val,hidden_val,show_val', [
        # both nipsa and hidden fields are set, so hide the annotation
        (True, True, False),
        # only nipsa field is set, still hide the annotation
        (True, False, False),
        # only hidden field is set, still hide the annotation
        (False, True, False),
        # neither field is set, don't hide the annotation
        (False, False, True),
    ])
    def test_visibility_of_moderated_and_nipsaed_annotations(
        self, index, factories, pyramid_request, search, user,
        AnnotationSearchIndexPresenter, nipsa_val, hidden_val, show_val
    ):

        pyramid_request.user = user
        search.append_modifier(query.HiddenFilter(pyramid_request))

        annotation_1 = factories.Annotation.build()
        annotation_2 = factories.Annotation.build(userid=user.userid)
        expected_ids = [annotation_2.id]

        if show_val:
            expected_ids.append(annotation_1.id)

        presenter = AnnotationSearchIndexPresenter.return_value
        presenter.asdict.return_value = {'id': annotation_1.id,
                                         'hidden': hidden_val,
                                         'nipsa': nipsa_val}
        index(annotation_1)

        presenter.asdict.return_value = {'id': annotation_2.id,
                                         'hidden': False,
                                         'nipsa': False}
        index(annotation_2)

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_hides_banned_users_annotations_from_other_users(
        self, pyramid_request, search, banned_user, user, Annotation
    ):
        pyramid_request.user = user
        search.append_modifier(query.HiddenFilter(pyramid_request))
        Annotation(userid=banned_user.userid)
        expected_ids = [Annotation(userid=user.userid).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_shows_banned_users_annotations_to_banned_user(
        self, pyramid_request, search, banned_user, user, Annotation
    ):
        pyramid_request.user = banned_user
        search.append_modifier(query.HiddenFilter(pyramid_request))
        expected_ids = [Annotation(userid=banned_user.userid).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    def test_shows_banned_users_annotations_in_groups_they_created(
        self, pyramid_request, search, banned_user, user, Annotation,
        group_service,
    ):
        pyramid_request.user = user
        group_service.groupids_created_by.return_value = ["created_by_banneduser"]
        search.append_modifier(query.HiddenFilter(pyramid_request))
        expected_ids = [Annotation(groupid="created_by_banneduser",
                                   userid=banned_user.userid).id]

        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_ids)

    @pytest.fixture
    def banned_user(self, factories):
        return factories.User(username="banned", nipsa=True)

    @pytest.fixture
    def user(self, factories):
        return factories.User(username="notbanned", nipsa=False)

    @pytest.fixture
    def pyramid_config(self, pyramid_config, banned_user):
        # Fake implementation of the `AnnotationTransformEvent` subscriber
        # which adds the "nipsa" flag to annotations during indexing.
        def add_nipsa_flag(event):
            if event.annotation.userid == banned_user.userid:
                event.annotation_dict['nipsa'] = True
        pyramid_config.add_subscriber(add_nipsa_flag, 'h.events.AnnotationTransformEvent')

        return pyramid_config

    @pytest.fixture
    def group_service(self, group_service):
        group_service.groupids_created_by.return_value = []
        return group_service


@pytest.mark.usefixtures('moderation_service')
class TestAnyMatcher(object):
    def test_matches_uriparts(self, search, Annotation):
        Annotation(target_uri="http://bar.com")
        matched_ids = [Annotation(target_uri="http://foo.com").id,
                       Annotation(target_uri="http://foo.com/bar").id]

        result = search.run({"any": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_matches_quote(self, search, Annotation):
        Annotation(target_selectors=[{'exact': 'selected bar text'}])
        matched_ids = [Annotation(target_selectors=[{'exact': 'selected foo text'}]).id,
                       Annotation(target_selectors=[{'exact': 'selected foo bar text'}]).id]

        result = search.run({"any": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_matches_text(self, search, Annotation):
        Annotation(text="bar is best")
        matched_ids = [Annotation(text="foo is fun").id,
                       Annotation(text="foo is bar's friend").id]

        result = search.run({"any": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_matches_tags(self, search, Annotation):
        Annotation(tags=["bar"])
        matched_ids = [Annotation(tags=["foo"]).id,
                       Annotation(tags=["foo", "bar"]).id]

        result = search.run({"any": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_ors_any_matches(self, search, Annotation):
        """
        Any is expected to match any of the following fields;
        quote, text, uri.parts, and tags
        that contain any of the passed keywords.
        """
        Annotation(target_selectors=[{'exact': 'selected baz text'}])
        Annotation(tags=["baz"])
        Annotation(target_uri="baz.com")
        Annotation(text="baz is best")

        matched_ids = [Annotation(target_uri="foo/bar/baz.com").id,
                       Annotation(target_selectors=[{'exact': 'selected foo text'}]).id,
                       Annotation(text="bar is best").id,
                       Annotation(tags=["foo"]).id]

        params = webob.multidict.MultiDict()
        params.add("any", "foo")
        params.add("any", "bar")
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.AnyMatcher())
        return search

    @pytest.fixture
    def Annotation(self, Annotation):
        # Override the default randomly-generated values for fields which
        # "any" matches against to ensure that we do not get unexpected
        # matches in tests. This will need to be modified if new fields are
        # added to the set which "any" matches against.
        def AnnotationWithDefaults(*args, **kwargs):
            kwargs.setdefault('tags', [])
            kwargs.setdefault('target_selectors', [{'exact': 'quotedoesnotmatch'}])
            kwargs.setdefault('target_uri', 'http://uridoesnotmatch.com')
            kwargs.setdefault('text', '')
            return Annotation(*args, **kwargs)
        return AnnotationWithDefaults


@pytest.mark.usefixtures('moderation_service')
class TestTagsMatcher(object):
    def test_matches_tag_key(self, search, Annotation):
        Annotation(shared=True)
        Annotation(shared=True, tags=["bar"])
        matched_ids = [Annotation(shared=True, tags=["foo"]).id,
                       Annotation(shared=True, tags=["foo", "bar"]).id]

        result = search.run({"tag": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_matches_tags_key(self, search, Annotation):
        Annotation(shared=True)
        Annotation(shared=True, tags=["bar"])
        matched_ids = [Annotation(shared=True, tags=["foo"]).id,
                       Annotation(shared=True, tags=["foo", "bar"]).id]

        result = search.run({"tags": "foo"})

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    def test_ands_multiple_tag_keys(self, search, Annotation):
        Annotation(shared=True)
        Annotation(shared=True, tags=["bar"])
        Annotation(shared=True, tags=["baz"])
        Annotation(shared=True, tags=["boo"])
        matched_ids = [Annotation(shared=True, tags=["foo", "baz", "fie", "boo"]).id,
                       Annotation(shared=True, tags=["foo", "baz", "fie", "boo", "bar"]).id]

        params = webob.multidict.MultiDict()
        params.add("tags", "foo")
        params.add("tags", "boo")
        params.add("tag", "fie")
        params.add("tag", "baz")
        result = search.run(params)

        assert sorted(result.annotation_ids) == sorted(matched_ids)

    @pytest.fixture
    def search(self, search):
        search.append_modifier(query.TagsMatcher())
        return search


@pytest.mark.usefixtures('moderation_service')
class TestRepliesMatcher(object):
    def test_matches_unnested_replies_to_annotations(self, Annotation, search):
        ann1 = Annotation()
        ann2 = Annotation()
        ann3 = Annotation()
        Annotation()
        # Create two replies on ann1.
        reply1 = Annotation(references=[ann1.id])
        reply2 = Annotation(references=[ann1.id])
        # Create a reply on ann2
        reply3 = Annotation(references=[ann2.id])
        # Create a reply on ann3
        Annotation(references=[ann3.id])

        expected_reply_ids = [reply1.id, reply2.id, reply3.id]

        ann_ids = [ann1.id, ann2.id]
        search.append_modifier(query.RepliesMatcher(ann_ids))
        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_reply_ids)

    def test_matches_replies_of_replies_to_an_annotation(self, Annotation, search):
        ann1 = Annotation()
        # Create a reply on ann1 and a reply to the reply.
        reply1 = Annotation(references=[ann1.id])
        reply2 = Annotation(references=[ann1.id, reply1.id])

        expected_reply_ids = [reply1.id, reply2.id]

        ann_ids = [ann1.id]
        search.append_modifier(query.RepliesMatcher(ann_ids))
        result = search.run({})

        assert sorted(result.annotation_ids) == sorted(expected_reply_ids)


@pytest.mark.usefixtures('moderation_service')
class TestTagsAggregation(object):
    def test_it_returns_annotation_counts_by_tag(self, Annotation, search):
        for i in range(2):
            Annotation(tags=["tag_a"])
        Annotation(tags=["tag_b"])

        search.append_aggregation(query.TagsAggregation())
        result = search.run({})

        tag_results = result.aggregations["tags"]
        count_for_tag_a = next(r for r in tag_results if r["tag"] == "tag_a")["count"]
        count_for_tag_b = next(r for r in tag_results if r["tag"] == "tag_b")["count"]

        assert len(tag_results) == 2
        assert count_for_tag_a == 2
        assert count_for_tag_b == 1

    def test_it_limits_number_of_annotation_counts_by_tag_returned(self, Annotation, search):
        bucket_limit = 2

        Annotation(tags=["tag_a"])
        for i in range(3):
            Annotation(tags=["tag_b"])
        for i in range(2):
            Annotation(tags=["tag_c"])

        search.append_aggregation(query.TagsAggregation(bucket_limit))
        result = search.run({})

        tag_results = result.aggregations["tags"]
        count_for_tag_b = next(r for r in tag_results if r["tag"] == "tag_b")["count"]
        count_for_tag_c = next(r for r in tag_results if r["tag"] == "tag_c")["count"]

        assert len(tag_results) == bucket_limit
        assert count_for_tag_b == 3
        assert count_for_tag_c == 2


@pytest.mark.usefixtures('moderation_service')
class TestUsersAggregation(object):
    def test_it_returns_annotation_counts_by_user(self, Annotation, search):
        for i in range(2):
            Annotation(userid="acct:pa@example.com")
        Annotation(userid="acct:pb@example.com")

        search.append_aggregation(query.UsersAggregation())
        result = search.run({})

        users_results = result.aggregations["users"]
        count_pa = next(r for r in users_results if r["user"] == "acct:pa@example.com")["count"]
        count_pb = next(r for r in users_results if r["user"] == "acct:pb@example.com")["count"]

        assert len(users_results) == 2
        assert count_pa == 2
        assert count_pb == 1

    def test_it_limits_number_of_annotation_counts_by_user_returned(self, Annotation, search):
        bucket_limit = 2

        Annotation(userid="acct:pa@example.com")
        for i in range(3):
            Annotation(userid="acct:pb@example.com")
        for i in range(2):
            Annotation(userid="acct:pc@example.com")

        search.append_aggregation(query.UsersAggregation(limit=bucket_limit))
        result = search.run({})

        users_results = result.aggregations["users"]
        count_pb = next(r for r in users_results if r["user"] == "acct:pb@example.com")["count"]
        count_pc = next(r for r in users_results if r["user"] == "acct:pc@example.com")["count"]

        assert len(users_results) == bucket_limit
        assert count_pb == 3
        assert count_pc == 2


@pytest.fixture
def search(pyramid_request):
    search = Search(pyramid_request)
    # Remove all default modifiers and aggregators except Sorter.
    search.clear()
    return search


@pytest.fixture
def es_dsl_search(pyramid_request):
    return elasticsearch_dsl.Search(
        using=pyramid_request.es.conn,
        index=pyramid_request.es.index,
    )


@pytest.fixture
def moderation_service(pyramid_config):
    svc = mock.create_autospec(AnnotationModerationService, spec_set=True, instance=True)
    svc.all_hidden.return_value = []
    svc.hidden.return_value = False
    pyramid_config.register_service(svc, name='annotation_moderation')
    return svc


@pytest.fixture
def AnnotationSearchIndexPresenter(patch):
    class_ = patch('h.search.index.presenters.AnnotationSearchIndexPresenter')
    class_.return_value.asdict.return_value = {'test': 'val'}
    return class_
