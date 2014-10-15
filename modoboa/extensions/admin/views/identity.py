"""
Identity related views.
"""
from django.shortcuts import render
from django.db import transaction
from django.utils.translation import ugettext as _, ungettext
from django.contrib.auth.decorators import (
    login_required, permission_required, user_passes_test
)
from django.views.decorators.csrf import ensure_csrf_cookie

import reversion

from modoboa.lib import parameters, events
from modoboa.lib.exceptions import (
    PermDeniedException, BadRequest
)
from modoboa.lib.webutils import (
    _render_to_string, render_to_json_response
)
from modoboa.lib.templatetags.lib_tags import pagination_bar
from modoboa.core.models import User
from modoboa.extensions.admin.models import Mailbox, Domain
from modoboa.lib.listing import (
    get_sort_order, get_listing_page
)
from modoboa.extensions.admin.lib import get_identities
from modoboa.extensions.admin.forms import (
    AccountForm
)


@login_required
@user_passes_test(
    lambda u: u.has_perm("core.add_user") or u.has_perm("admin.add_alias")
)
def _identities(request):
    filters = dict((fname, request.GET.get(fname, None))
                   for fname in ['searchquery', 'idtfilter', 'grpfilter'])
    request.session['identities_filters'] = filters
    idents_list = get_identities(request.user, **filters)
    sort_order, sort_dir = get_sort_order(request.GET, "identity",
                                          ["identity", "name_or_rcpt", "tags"])
    if sort_order in ["identity", "name_or_rcpt"]:
        objects = sorted(idents_list, key=lambda o: getattr(o, sort_order),
                         reverse=sort_dir == '-')
    else:
        objects = sorted(idents_list, key=lambda o: o.tags[0],
                         reverse=sort_dir == '-')
    context = {
        "handle_mailboxes": parameters.get_admin(
            "HANDLE_MAILBOXES", raise_error=False)
    }
    page = get_listing_page(objects, request.GET.get("page", 1))
    if page is None:
        context["length"] = 0
    else:
        context["rows"] = _render_to_string(
            request, "admin/identities_table.html", {
                "identities": page.object_list
            }
        )
        context["pages"] = [page.number]
    return render_to_json_response(context)


@login_required
@user_passes_test(
    lambda u: u.has_perm("admin.add_user") or u.has_perm("admin.add_alias")
)
@ensure_csrf_cookie
def identities(request, tplname="admin/identities.html"):
    return render(request, tplname, {
        "selection": "identities",
        "deflocation": "list/"
    })


@login_required
@permission_required("core.add_user")
def accounts_list(request):
    accs = User.objects.filter(is_superuser=False) \
        .exclude(groups__name='SimpleUsers')
    res = [a.username for a in accs.all()]
    return render_to_json_response(res)


@login_required
@permission_required("admin.add_mailbox")
def list_quotas(request, tplname="admin/quotas.html"):
    from modoboa.lib.dbutils import db_type

    sort_order, sort_dir = get_sort_order(request.GET, "address")
    mboxes = Mailbox.objects.get_for_admin(
        request.user, request.GET.get("searchquery", None)
    )
    mboxes = mboxes.exclude(quota=0)
    if sort_order in ["address", "quota", "quota_value__bytes"]:
        mboxes = mboxes.order_by("%s%s" % (sort_dir, sort_order))
    elif sort_order == "quota_usage":
        where = "admin_mailbox.address||'@'||admin_domain.name"
        db_type = db_type()
        if db_type == "postgres":
            select = '(admin_quota.bytes::float / (CAST(admin_mailbox.quota AS BIGINT) * 1048576)) * 100'
        else:
            select = 'admin_quota.bytes / (admin_mailbox.quota * 1048576) * 100'
            if db_type == "mysql":
                where = "CONCAT(admin_mailbox.address,'@',admin_domain.name)"
        mboxes = mboxes.extra(
            select={'quota_usage': select},
            where=["admin_quota.username=%s" % where],
            tables=["admin_quota", "admin_domain"],
            order_by=["%s%s" % (sort_dir, sort_order)]
        )
    else:
        raise BadRequest(_("Invalid request"))
    page = get_listing_page(mboxes, request.GET.get("page", 1))
    return render_to_json_response({
        "page": page.number,
        "paginbar": pagination_bar(page),
        "table": _render_to_string(request, tplname, {
            "mboxes": page
        })
    })


@login_required
@permission_required("core.add_user")
@transaction.commit_on_success
@reversion.create_revision()
def newaccount(request):
    """Create a new account.

    .. note:: An issue still remains int this code: if all validation
       steps are successful but an error occurs after we call 'save',
       the account will be created. It happens transaction management
       doesn't work very well with nested functions. Need to wait for
       django 1.6 and atomicity.
    """
    from modoboa.extensions.admin.forms import AccountWizard
    return AccountWizard(request).process()


@login_required
@permission_required("core.change_user")
@transaction.commit_on_success
@reversion.create_revision()
def editaccount(request, accountid, tplname="common/tabforms.html"):
    account = User.objects.get(pk=accountid)
    if not request.user.can_access(account):
        raise PermDeniedException
    mb = None
    if account.mailbox_set.count():
        mb = account.mailbox_set.all()[0]

    instances = dict(general=account, mail=mb, perms=account)
    events.raiseEvent("FillAccountInstances", request.user, account, instances)
    return AccountForm(request, instances=instances).process()


@login_required
@permission_required("core.delete_user")
@transaction.commit_on_success
def delaccount(request, accountid):
    keepdir = True if request.POST.get("keepdir", "false") == "true" else False
    User.objects.get(pk=accountid).delete(request.user, keep_mb_dir=keepdir)
    return render_to_json_response(
        ungettext("Account deleted", "Accounts deleted", 1)
    )


@login_required
@permission_required("admin.add_domain")
def remove_permission(request):
    domid = request.GET.get("domid", None)
    daid = request.GET.get("daid", None)
    if domid is None or daid is None:
        raise BadRequest(_("Invalid request"))
    try:
        account = User.objects.get(pk=daid)
        domain = Domain.objects.get(pk=domid)
    except (User.DoesNotExist, Domain.DoesNotExist):
        raise BadRequest(_("Invalid request"))
    if not request.user.can_access(account) or not request.user.can_access(domain):
        raise PermDeniedException
    domain.remove_admin(account)
    return render_to_json_response({})
