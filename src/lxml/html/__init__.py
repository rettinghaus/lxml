"""The ``lxml.html`` tool set for HTML handling.
"""

import threading
import re
import urlparse
import copy
from lxml import etree
from lxml.html import defs
from lxml import cssselect
from lxml.html.setmixin import SetMixin
try:
    from UserDict import DictMixin
except ImportError:
    # DictMixin was introduced in Python 2.4
    from lxml.html._dictmixin import DictMixin
import sets

__all__ = [
    'document_fromstring', 'fragment_fromstring', 'fragments_fromstring', 'fromstring',
    'tostring', 'Element', 'defs', 'open_in_browser', 'submit_form',
    'find_rel_links', 'find_class', 'make_links_absolute',
    'resolve_base_href', 'iterlinks', 'rewrite_links', 'open_in_browser']

_rel_links_xpath = etree.XPath("descendant-or-self::a[@rel]")
#_class_xpath = etree.XPath(r"descendant-or-self::*[regexp:match(@class, concat('\b', $class_name, '\b'))]", {'regexp': 'http://exslt.org/regular-expressions'})
_class_xpath = etree.XPath("descendant-or-self::*[@class and contains(concat(' ', normalize-space(@class), ' '), concat(' ', $class_name, ' '))]")
_id_xpath = etree.XPath("descendant-or-self::*[@id=$id]")
_collect_string_content = etree.XPath("string()")
_css_url_re = re.compile(r'url\((.*?)\)', re.I)
_css_import_re = re.compile(r'@import "(.*?)"')
_label_xpath = etree.XPath("//label[@for=$id]")
_archive_re = re.compile(r'[^ ]+')

class HtmlMixin(object):

    def base_url(self):
        """
        Returns the base URL, given when the page was parsed.

        Use with ``urlparse.urljoin(el.base_url, href)`` to get
        absolute URLs.
        """
        return self.getroottree().docinfo.URL
    base_url = property(base_url, doc=base_url.__doc__)

    def forms(self):
        """
        Return a list of all the forms
        """
        return list(self.getiterator('form'))
    forms = property(forms, doc=forms.__doc__)

    def body(self):
        """
        Return the <body> element.  Can be called from a child element
        to get the document's head.
        """
        return self.xpath('//body')[0]
    body = property(body, doc=body.__doc__)

    def head(self):
        """
        Returns the <head> element.  Can be called from a child
        element to get the document's head.
        """
        return self.xpath('//head')[0]
    head = property(head, doc=head.__doc__)

    def label__get(self):
        """
        Get or set any <label> element associated with this element.
        """
        id = self.get('id')
        if not id:
            return None
        result = _label_xpath(self, id=id)
        if not result:
            return None
        else:
            return result[0]
    def label__set(self, label):
        id = self.get('id')
        if not id:
            raise TypeError(
                "You cannot set a label for an element (%r) that has no id"
                % self)
        if not label.tag == 'label':
            raise TypeError(
                "You can only assign label to a label element (not %r)"
                % label)
        label.set('for', id)
    def label__del(self):
        label = self.label
        if label is not None:
            del label.attrib['for']
    label = property(label__get, label__set, label__del, doc=label__get.__doc__)

    def drop_tree(self):
        """
        Removes this element from the tree, including its children and
        text.  The tail text is joined to the previous element or
        parent.
        """
        parent = self.getparent()
        assert parent is not None
        if self.tail:
            previous = self.getprevious()
            if previous is None:
                parent.text = (parent.text or '') + self.tail
            else:
                previous.tail = (previous.tail or '') + self.tail
        parent.remove(self)

    def drop_tag(self):
        """
        Remove the tag, but not its children or text.  The children and text
        are merged into the parent.

        Example::

            >>> h = fragment_fromstring('<div>Hello <b>World!</b></div>')
            >>> h.find('//b').drop_tag()
            >>> print tostring(h)
            <div>Hello World!</div>
        """
        parent = self.getparent()
        assert parent is not None
        previous = self.getprevious()
        if self.text and isinstance(self.tag, basestring):
            # not a Comment, etc.
            if previous is None:
                parent.text = (parent.text or '') + self.text
            else:
                previous.tail = (previous.tail or '') + self.text
        if self.tail:
            if len(self):
                last = self[-1]
                last.tail = (last.tail or '') + self.tail
            elif previous is None:
                parent.text = (parent.text or '') + self.tail
            else:
                previous.tail = (previous.tail or '') + self.tail
        index = parent.index(self)
        parent[index:index+1] = self[:]

    def find_rel_links(self, rel):
        """
        Find any links like ``<a rel="{rel}">...</a>``; returns a list of elements.
        """
        rel = rel.lower()
        return [el for el in _rel_links_xpath(self)
                if el.get('rel').lower() == rel]

    def find_class(self, class_name):
        """
        Find any elements with the given class name.
        """
        return _class_xpath(self, class_name=class_name)

    def get_element_by_id(self, id, *default):
        """
        Get the first element in a document with the given id.  If none is
        found, return the default argument if provided or raise KeyError
        otherwise.

        Note that there can be more than one element with the same id,
        and this isn't uncommon in HTML documents found in the wild.
        Browsers return only the first match, and this function does
        the same.
        """
        try:
            # FIXME: should this check for multiple matches?
            # browsers just return the first one
            return _id_xpath(self, id=id)[0]
        except IndexError:
            if default:
                return default[0]
            else:
                raise KeyError, id

    def text_content(self):
        """
        Return the text content of the tag (and the text in any children).
        """
        return _collect_string_content(self)

    def cssselect(self, expr):
        """
        Run the CSS expression on this element and its children,
        returning a list of the results.

        Equivalent to lxml.cssselect.CSSSelect(expr)(self) -- note
        that pre-compiling the expression can provide a substantial
        speedup.
        """
        return cssselect.CSSSelect(expr)(self)

    ########################################
    ## Link functions
    ########################################

    def make_links_absolute(self, base_url=None, resolve_base_href=True):
        """
        Make all links in the document absolute, given the
        ``base_url`` for the document (the full URL where the document
        came from), or if no ``base_url`` is given, then the ``.base_url`` of the document.

        If ``resolve_base_href`` is true, then any ``<base href>``
        tags in the document are used *and* removed from the document.
        If it is false then any such tag is ignored.
        """
        if base_url is None:
            base_url = self.base_url
            if base_url is None:
                raise TypeError(
                    "No base_url given, and the document has no base_url")
        if resolve_base_href:
            self.resolve_base_href()
        def link_repl(href):
            return urlparse.urljoin(base_url, href)
        self.rewrite_links(link_repl)

    def resolve_base_href(self):
        """
        Find any ``<base href>`` tag in the document, and apply its
        values to all links found in the document.  Also remove the
        tag once it has been applied.
        """
        base_href = None
        basetags = self.xpath('//base[@href]')
        for b in basetags:
            base_href = b.get('href')
            b.drop_tree()
        if not base_href:
            return
        self.make_links_absolute(base_href, resolve_base_href=False)
        
    def iterlinks(self):
        """
        Yield (element, attribute, link, pos), where attribute may be None
        (indicating the link is in the text).  ``pos`` is the position
        where the link occurs; often 0, but sometimes something else in
        the case of links in stylesheets or style tags.

        Note: <base href> is *not* taken into account in any way.  The
        link you get is exactly the link in the document.
        """
        link_attrs = defs.link_attrs
        for el in self.getiterator():
            attribs = el.attrib
            if el.tag != 'object':
                for attrib in link_attrs:
                    if attrib in attribs:
                        yield (el, attrib, attribs[attrib], 0)
            elif el.tag == 'object':
                codebase = None
                ## <object> tags have attributes that are relative to
                ## codebase
                if 'codebase' in attribs:
                    codebase = el.get('codebase')
                    yield (el, 'codebase', codebase, 0)
                for attrib in 'classid', 'data':
                    if attrib in attribs:
                        value = el.get(attrib)
                        if codebase is not None:
                            value = urlparse.urljoin(codebase, value)
                        yield (el, attrib, value, 0)
                if 'archive' in attribs:
                    for match in _archive_re.finditer(el.get('archive')):
                        value = match.group(0)
                        if codebase is not None:
                            value = urlparse.urljoin(codebase, value)
                        yield (el, 'archive', value, match.start())
            if el.tag == 'param':
                valuetype = el.get('valuetype') or ''
                if valuetype.lower() == 'ref':
                    ## FIXME: while it's fine we *find* this link,
                    ## according to the spec we aren't supposed to
                    ## actually change the value, including resolving
                    ## it.  It can also still be a link, even if it
                    ## doesn't have a valuetype="ref" (which seems to be the norm)
                    ## http://www.w3.org/TR/html401/struct/objects.html#adef-valuetype
                    yield (el, 'value', el.get('value'), 0)
            if el.tag == 'style' and el.text:
                for match in _css_url_re.finditer(el.text):
                    yield (el, None, match.group(1), match.start(1))
                for match in _css_import_re.finditer(el.text):
                    yield (el, None, match.group(1), match.start(1))
            if 'style' in attribs:
                for match in _css_url_re.finditer(attribs['style']):
                    yield (el, 'style', match.group(1), match.start(1))

    def rewrite_links(self, link_repl_func, resolve_base_href=True,
                      base_href=None):
        """
        Rewrite all the links in the document.  For each link
        ``link_repl_func(link)`` will be called, and the return value
        will replace the old link.

        Note that links may not be absolute (unless you first called
        ``make_links_absolute()``), and may be internal (e.g.,
        ``'#anchor'``).  They can also be values like
        ``'mailto:email'`` or ``'javascript:expr'``.

        If you give ``base_href`` then all links passed to
        ``link_repl_func()`` will take that into account.

        If the ``link_repl_func`` returns None, the attribute or
        tag text will be removed completely.
        """
        if base_href is not None:
            # FIXME: this can be done in one pass with a wrapper
            # around link_repl_func
            self.make_links_absolute(base_href, resolve_base_href=resolve_base_href)
        elif resolve_base_href:
            self.resolve_base_href()
        for el, attrib, link, pos in self.iterlinks():
            new_link = link_repl_func(link)
            if new_link == link:
                continue
            if new_link is None:
                # Remove the attribute or element content
                if attrib is None:
                    el.text = ''
                else:
                    del el.attrib[attrib]
                continue
            if attrib is None:
                new = el.text[:pos] + new_link + el.text[pos+len(link):]
                el.text = new
            else:
                cur = el.attrib[attrib]
                if not pos and len(cur) == len(link):
                    # Most common case
                    el.attrib[attrib] = new_link
                else:
                    new = cur[:pos] + new_link + cur[pos+len(link):]
                    el.attrib[attrib] = new
                    

class _MethodFunc(object):
    """
    An object that represents a method on an element as a function;
    the function takes either an element or an HTML string.  It
    returns whatever the function normally returns, or if the function
    works in-place (and so returns None) it returns a serialized form
    of the resulting document.
    """
    def __init__(self, name, copy=False, source_class=HtmlMixin):
        self.name = name
        self.copy = copy
        self.__doc__ = getattr(source_class, self.name).__doc__
    def __call__(self, doc, *args, **kw):
        if isinstance(doc, basestring):
            if 'copy' in kw:
                raise TypeError(
                    "The keyword 'copy' can only be used with element inputs to %s, not a string input" % self.name)
            return_string = True
            doc = fromstring(doc, **kw)
        else:
            if 'copy' in kw:
                copy = kw.pop('copy')
            else:
                copy = self.copy
            return_string = False
            if copy:
                doc = copy.deepcopy(doc)
        meth = getattr(doc, self.name)
        result = meth(*args, **kw)
        # FIXME: this None test is a bit sloppy 
        if result is None:
            # Then return what we got in
            if return_string:
                return tostring(doc)
            else:
                return doc
        else:
            return result

find_rel_links = _MethodFunc('find_rel_links', copy=False)
find_class = _MethodFunc('find_class', copy=False)
make_links_absolute = _MethodFunc('make_links_absolute', copy=True)
resolve_base_href = _MethodFunc('resolve_base_href', copy=True)
iterlinks = _MethodFunc('iterlinks', copy=False)
rewrite_links = _MethodFunc('rewrite_links', copy=True)

class HtmlComment(etree.CommentBase, HtmlMixin):
    pass

class HtmlElement(etree.ElementBase, HtmlMixin):
    pass

class HtmlProcessingInstruction(etree.PIBase, HtmlMixin):
    pass

class HtmlEntity(etree.EntityBase, HtmlMixin):
    pass


class HtmlElementClassLookup(etree.CustomElementClassLookup):
    """A lookup scheme for HTML Element classes.

    To create a lookup instance with different Element classes, pass a tag
    name mapping of Element classes in the ``classes`` keyword argument and/or
    a tag name mapping of Mixin classes in the ``mixins`` keyword argument.
    The special key '*' denotes a Mixin class that should be mixed into all
    Element classes.
    """
    _default_element_classes = {}

    def __init__(self, classes=None, mixins=None):
        etree.CustomElementClassLookup.__init__(self)
        if classes is None:
            classes = self._default_element_classes.copy()
        if mixins:
            mixers = {}
            for name, value in mixins:
                if name == '*':
                    for n in classes.keys():
                        mixers.setdefault(n, []).append(value)
                else:
                    mixers.setdefault(name, []).append(value)
            for name, mix_bases in mixers.items():
                cur = classes.get(name, HtmlElement)
                bases = tuple(mix_bases + [cur])
                classes[name] = type(cur.__name__, bases, {})
        self._element_classes = classes

    def lookup(self, node_type, document, namespace, name):
        if node_type == 'element':
            return self._element_classes.get(name.lower(), HtmlElement)
        elif node_type == 'comment':
            return HtmlComment
        elif node_type == 'PI':
            return HtmlProcessingInstruction
        elif node_type == 'entity':
            return HtmlEntity
        # Otherwise normal lookup
        return None

################################################################################
# parsing
################################################################################

def document_fromstring(html, **kw):
    value = etree.HTML(html, html_parser, **kw)
    if value is None:
        raise etree.ParserError(
            "Document is empty")
    return value

def fragments_fromstring(html, no_leading_text=False, **kw):
    """
    Parses several HTML elements, returning a list of elements.

    The first item in the list may be a string (though leading
    whitespace is removed).  If no_leading_text is true, then it will
    be an error if there is leading text, and it will always be a list
    of only elements.
    """
    # FIXME: check what happens when you give html with a body, head, etc.
    start = html[:20].lstrip().lower()
    if not start.startswith('<html') and not start.startswith('<!doctype'):
        html = '<html><body>%s</body></html>' % html
    doc = document_fromstring(html, **kw)
    assert doc.tag == 'html'
    bodies = [e for e in doc if e.tag == 'body']
    assert len(bodies) == 1, ("too many bodies: %r in %r" % (bodies, html))
    body = bodies[0]
    elements = []
    if no_leading_text and body.text and body.text.strip():
        raise etree.ParserError(
            "There is leading text: %r" % body.text)
    if body.text and body.text.strip():
        elements.append(body.text)
    elements.extend(body)
    # FIXME: removing the reference to the parent artificial document
    # would be nice
    return elements

def fragment_fromstring(html, create_parent=False, **kw):
    """
    Parses a single HTML element; it is an error if there is more than
    one element, or if anything but whitespace precedes or follows the
    element.

    If create_parent is true (or is a tag name) then a parent node
    will be created to encapsulate the HTML in a single element.
    """
    if create_parent:
        if not isinstance(create_parent, basestring):
            create_parent = 'div'
        return fragment_fromstring('<%s>%s</%s>' % (
            create_parent, html, create_parent), **kw)
    elements = fragments_fromstring(html, no_leading_text=True)
    if not elements:
        raise etree.ParserError(
            "No elements found")
    if len(elements) > 1:
        raise etree.ParserError(
            "Multiple elements found (%s)"
            % ', '.join([_element_name(e) for e in elements]))
    el = elements[0]
    if el.tail and el.tail.strip():
        raise etree.ParserError(
            "Element followed by text: %r" % el.tail)
    el.tail = None
    return el

def fromstring(html, **kw):
    """
    Parse the html, returning a single element/document.

    This tries to minimally parse the chunk of text, without knowing if it
    is a fragment or a document.
    """
    start = html[:10].lstrip().lower()
    if start.startswith('<html') or start.startswith('<!doctype'):
        # Looks like a full HTML document
        return document_fromstring(html, **kw)
    # otherwise, lets parse it out...
    doc = document_fromstring(html, **kw)
    bodies = doc.findall('body')
    if bodies:
        body = bodies[0]
        if len(bodies) > 1:
            # Somehow there are multiple bodies, which is bad, but just
            # smash them into one body
            for other_body in bodies[1:]:
                if other_body.text:
                    if len(body):
                        body[-1].tail = (body[-1].tail or '') + other_body.text
                    else:
                        body.text = (body.text or '') + other_body.text
                body.extend(other_body)
                # We'll ignore tail
                # I guess we are ignoring attributes too
                other_body.drop_tree()
    else:
        body = None
    heads = doc.findall('head')
    if heads:
        # Well, we have some sort of structure, so lets keep it all
        head = heads[0]
        if len(heads) > 1:
            for other_head in heads[1:]:
                head.extend(other_head)
                # We don't care about text or tail in a head
                other_head.drop_tree()
        return doc
    if (len(body) == 1 and (not body.text or not body.text.strip())
        and (not body[-1].tail or not body[-1].tail.strip())):
        # The body has just one element, so it was probably a single
        # element passed in
        return body[0]
    # Now we have a body which represents a bunch of tags which have the
    # content that was passed in.  We will create a fake container, which
    # is the body tag, except <body> implies too much structure.
    if _contains_block_level_tag(body):
        body.tag = 'div'
    else:
        body.tag = 'span'
    return body

def parse(filename, parser=None, **kw):
    """
    Parse a filename, URL, or file-like object into an HTML document.

    You may pass the keyword argument ``base_url='http://...'`` to set
    the base URL.
    """
    if parser is None:
        parser = html_parser
    return etree.parse(filename, parser, **kw)

def _contains_block_level_tag(el):
    # FIXME: I could do this with XPath, but would that just be
    # unnecessarily slow?
    for el in el.getiterator():
        if el.tag in defs.block_tags:
            return True
    return False

def _element_name(el):
    if isinstance(el, etree.CommentBase):
        return 'comment'
    elif isinstance(el, basestring):
        return 'string'
    else:
        return el.tag

################################################################################
# form handling
################################################################################

class FormElement(HtmlElement):
    """
    Represents a <form> element.
    """

    def inputs(self):
        """
        Returns an accessor for all the input elements in the form.

        See `InputGetter` for more information about the object.
        """
        return InputGetter(self)
    inputs = property(inputs, doc=inputs.__doc__)

    def fields__get(self):
        """
        Dictionary-like object that represents all the fields in this
        form.  You can set values in this dictionary to effect the
        form.
        """
        return FieldsDict(self.inputs)
    def fields__set(self, value):
        prev_keys = self.fields.keys()
        for key, value in value.iteritems():
            if key in prev_keys:
                prev_keys.remove(key)
            self.fields[key] = value
        for key in prev_keys:
            if key is None:
                # Case of an unnamed input; these aren't really
                # expressed in form_values() anyway.
                continue
            self.fields[key] = None

    fields = property(fields__get, fields__set, doc=fields__get.__doc__)

    def _name(self):
        if self.get('name'):
            return self.get('name')
        elif self.get('id'):
            return '#' + self.get('id')
        return str(self.body.findall('form').index(self))

    def form_values(self):
        """
        Return a list of tuples of the field values for the form.
        This is suitable to be passed to ``urllib.urlencode()``.
        """
        results = []
        for el in self.inputs:
            name = el.name
            if not name:
                continue
            if el.tag == 'textarea':
                results.append((name, el.value))
            elif el.tag == 'select':
                value = el.value
                if el.multiple:
                    for v in value:
                        results.append((name, v))
                elif value is not None:
                    results.append((name, el.value))
            else:
                assert el.tag == 'input', (
                    "Unexpected tag: %r" % el)
                if el.checkable and not el.checked:
                    continue
                if el.type in ('submit', 'image', 'reset'):
                    continue
                value = el.value
                if value is not None:
                    results.append((name, el.value))
        return results

    def action__get(self):
        """
        Get/set the form's ``action`` attribute.
        """
        base_url = self.base_url
        action = self.get('action')
        if base_url and action is not None:
            return urlparse.urljoin(base_url, action)
        else:
            return action
    def action__set(self, value):
        self.set('action', value)
    def action__del(self):
        if 'action' in self.attrib:
            del self.attrib['action']
    action = property(action__get, action__set, action__del, doc=action__get.__doc__)

    def method__get(self):
        """
        Get/set the form's method.  Always returns a capitalized
        string, and defaults to ``'GET'``
        """
        return self.get('method', 'GET').upper()
    def method__set(self, value):
        self.set('method', value.upper())
    method = property(method__get, method__set, doc=method__get.__doc__)

HtmlElementClassLookup._default_element_classes['form'] = FormElement

def submit_form(form, extra_values=None, open_http=None):
    """
    Helper function to submit a form.  Returns a file-like object, as from
    ``urllib.urlopen()``.  This object also has a ``.geturl()`` function,
    which shows the URL if there were any redirects.

    You can use this like::

        >>> form = doc.forms[0]
        >>> form.inputs['foo'].value = 'bar' # etc
        >>> response = form.submit()
        >>> doc = parse(response)
        >>> doc.make_links_absolute(response.geturl())

    To change the HTTP requester, pass a function as ``open_http`` keyword
    argument that opens the URL for you.  The function must have the following
    signature::

        open_http(method, URL, values)

    The action is one of 'GET' or 'POST', the URL is the target URL as a
    string, and the values are a sequence of ``(name, value)`` tuples with the
    form data.
    """
    values = form.form_values()
    if extra_values:
        if hasattr(extra_values, 'items'):
            extra_values = extra_values.items()
        values.extend(extra_values)
    if open_http is None:
        open_http = open_http_urllib
    return open_http(form.method, form.action, values)

def open_http_urllib(method, url, values):
    import urllib
    ## FIXME: should test that it's not a relative URL or something
    if method == 'GET':
        if '?' in url:
            url += '&'
        else:
            url += '?'
        url += urllib.urlencode(values)
        data = None
    else:
        data = urllib.urlencode(values)
    return urllib.urlopen(url, data)

class FieldsDict(DictMixin):

    def __init__(self, inputs):
        self.inputs = inputs
    def __getitem__(self, item):
        return self.inputs[item].value
    def __setitem__(self, item, value):
        self.inputs[item].value = value
    def __delitem__(self, item):
        raise KeyError(
            "You cannot remove keys from ElementDict")
    def keys(self):
        return self.inputs.keys()
    def __contains__(self, item):
        return item in self.inputs

    def __repr__(self):
        return '<%s for form %s>' % (
            self.__class__.__name__,
            self.inputs.form._name())

class InputGetter(object):

    """
    An accessor that represents all the input fields in a form.

    You can get fields by name from this, with
    ``form.inputs['field_name']``.  If there are a set of checkboxes
    with the same name, they are returned as a list (a `CheckboxGroup`
    which also allows value setting).  Radio inputs are handled
    similarly.

    You can also iterate over this to get all input elements.  This
    won't return the same thing as if you get all the names, as
    checkboxes and radio elements are returned individually.
    """

    _name_xpath = etree.XPath(".//*[@name = $name and (name(.) = 'select' or name(.) = 'input' or name(.) = 'textarea')]")
    _all_xpath = etree.XPath(".//*[name() = 'select' or name() = 'input' or name() = 'textarea']")

    def __init__(self, form):
        self.form = form

    def __repr__(self):
        return '<%s for form %s>' % (
            self.__class__.__name__,
            self.form._name())

    ## FIXME: there should be more methods, and it's unclear if this is
    ## a dictionary-like object or list-like object

    def __getitem__(self, name):
        results = self._name_xpath(self.form, name=name)
        if results:
            type = results[0].get('type')
            if type == 'radio' and len(results) > 1:
                group = RadioGroup(results)
                group.name = name
                return group
            elif type == 'checkbox' and len(results) > 1:
                group = CheckboxGroup(results)
                group.name = name
                return group
            else:
                # I don't like throwing away elements like this
                return results[0]
        else:
            raise KeyError(
                "No input element with the name %r" % name)

    def __contains__(self, name):
        results = self._name_xpath(self.form, name=name)
        return bool(results)

    def keys(self):
        names = sets.Set()
        for el in self:
            if el.name is not None:
                names.add(el.name)
        return list(names)

    def __iter__(self):
        ## FIXME: kind of dumb to turn a list into an iterator, only
        ## to have it likely turned back into a list again :(
        return iter(self._all_xpath(self.form))

class InputMixin(object):

    """
    Mix-in for all input elements (input, select, and textarea)
    """


    def name__get(self):
        """
        Get/set the name of the element
        """
        return self.get('name')
    def name__set(self, value):
        self.set('name', value)
    def name__del(self):
        if 'name' in self.attrib:
            del self.attrib['name']
    name = property(name__get, name__set, name__del, doc=name__get.__doc__)

    def __repr__(self):
        type = getattr(self, 'type', None)
        if type:
            type = ' type=%r' % type
        else:
            type = ''
        return '<%s %x name=%r%s>' % (
            self.__class__.__name__, id(self), self.name, type)
    
class TextareaElement(InputMixin, HtmlElement):
    """
    ``<textarea>`` element.  You can get the name with ``.name`` and
    get/set the value with ``.value``
    """

    def value__get(self):
        """
        Get/set the value (which is the contents of this element)
        """
        return self.text or ''
    def value__set(self, value):
        self.text = value
    def value__del(self):
        self.text = ''
    value = property(value__get, value__set, value__del, doc=value__get.__doc__)

HtmlElementClassLookup._default_element_classes['textarea'] = TextareaElement

class SelectElement(InputMixin, HtmlElement):
    """
    ``<select>`` element.  You can get the name with ``.name``.

    ``.value`` will be the value of the selected option, unless this
    is a multi-select element (``<select multiple>``), in which case
    it will be a set-like object.  In either case ``.value_options``
    gives the possible values.

    The boolean attribute ``.multiple`` shows if this is a
    multi-select.
    """

    def value__get(self):
        """
        Get/set the value of this select (the selected option).

        If this is a multi-select, this is a set-like object that
        represents all the selected options.
        """
        if self.multiple:
            return MultipleSelectOptions(self)
        for el in self.getiterator('option'):
            if 'selected' in el.attrib:
                value = el.get('value')
                # FIXME: If value is None, what to return?, get_text()?
                return value
        return None

    def value__set(self, value):
        if self.multiple:
            if isinstance(value, basestring):
                raise TypeError(
                    "You must pass in a sequence")
            self.value.clear()
            self.value.update(value)
            return
        if value is not None:
            for el in self.getiterator('option'):
                # FIXME: also if el.get('value') is None?
                if el.get('value') == value:
                    checked_option = el
                    break
            else:
                raise ValueError(
                    "There is no option with the value of %r" % value)
        for el in self.getiterator('option'):
            if 'selected' in el.attrib:
                del el.attrib['selected']
        if value is not None:
            checked_option.set('selected', '')

    def value__del(self):
        # FIXME: should del be allowed at all?
        if self.multiple:
            self.value.clear()
        else:
            self.value = None

    value = property(value__get, value__set, value__del, doc=value__get.__doc__)

    def value_options(self):
        """
        All the possible values this select can have (the ``value``
        attribute of all the ``<option>`` elements.
        """
        return [el.get('value') for el in self.getiterator('option')]
    value_options = property(value_options, doc=value_options.__doc__)

    def multiple__get(self):
        """
        Boolean attribute: is there a ``multiple`` attribute on this element.
        """
        return 'multiple' in self.attrib
    def multiple__set(self, value):
        if value:
            self.set('multiple', '')
        elif 'multiple' in self.attrib:
            del self.attrib['multiple']
    multiple = property(multiple__get, multiple__set, doc=multiple__get.__doc__)

HtmlElementClassLookup._default_element_classes['select'] = SelectElement

class MultipleSelectOptions(SetMixin):
    """
    Represents all the selected options in a ``<select multiple>`` element.

    You can add to this set-like option to select an option, or remove
    to unselect the option.
    """

    def __init__(self, select):
        self.select = select

    def options(self):
        """
        Iterator of all the ``<option>`` elements.
        """
        return self.select.getiterator('option')
    options = property(options)

    def __iter__(self):
        for option in self.options:
            yield option.get('value')

    def add(self, item):
        for option in self.options:
            if option.get('value') == item:
                option.set('selected', '')
                break
        else:
            raise ValueError(
                "There is no option with the value %r" % item)

    def remove(self, item):
        for option in self.options:
            if option.get('value') == item:
                if 'selected' in option.attrib:
                    del option.attrib['selected']
                else:
                    raise ValueError(
                        "The option %r is not currently selected" % item)
                break
        else:
            raise ValueError(
                "There is not option with the value %r" % item)

    def __repr__(self):
        return '<%s {%s} for select name=%r>' % (
            self.__class__.__name__,
            ', '.join([repr(v) for v in self]),
            self.select.name)

class RadioGroup(list):
    """
    This object represents several ``<input type=radio>`` elements
    that have the same name.

    You can use this like a list, but also use the property
    ``.value`` to check/uncheck inputs.  Also you can use
    ``.value_options`` to get the possible values.
    """

    def value__get(self):
        """
        Get/set the value, which checks the radio with that value (and
        unchecks any other value).
        """
        for el in self:
            if 'checked' in el.attrib:
                return el.get('value')
        return None

    def value__set(self, value):
        if value is not None:
            for el in self:
                if el.get('value') == value:
                    checked_option = el
                    break
            else:
                raise ValueError(
                    "There is no radio input with the value %r" % value)
        for el in self:
            if 'checked' in el.attrib:
                del el.attrib['checked']
        if value is not None:
            checked_option.set('checked', '')

    def value__del(self):
        self.value = None

    value = property(value__get, value__set, value__del, doc=value__get.__doc__)

    def value_options(self):
        """
        Returns a list of all the possible values.
        """
        return [el.get('value') for el in self]
    value_options = property(value_options, doc=value_options.__doc__)

    def __repr__(self):
        return '%s(%s)' % (
            self.__class__.__name__,
            list.__repr__(self))

class CheckboxGroup(list):
    """
    Represents a group of checkboxes (``<input type=checkbox>``) that
    have the same name.

    In addition to using this like a list, the ``.value`` attribute
    returns a set-like object that you can add to or remove from to
    check and uncheck checkboxes.  You can also use ``.value_options``
    to get the possible values.
    """

    def value__get(self):
        """
        Return a set-like object that can be modified to check or
        uncheck individual checkboxes according to their value.
        """
        return CheckboxValues(self)
    def value__set(self, value):
        self.value.clear()
        if not hasattr(value, '__iter__'):
            raise ValueError(
                "A CheckboxGroup (name=%r) must be set to a sequence (not %r)"
                % (self[0].name, value))
        self.value.update(value)
    def value__del(self):
        self.value.clear()
    value = property(value__get, value__set, value__del, doc=value__get.__doc__)

    def __repr__(self):
        return '%s(%s)' % (
            self.__class__.__name__, list.__repr__(self))

class CheckboxValues(SetMixin):

    """
    Represents the values of the checked checkboxes in a group of
    checkboxes with the same name.
    """

    def __init__(self, group):
        self.group = group

    def __iter__(self):
        return iter([
            el.get('value')
            for el in self.group
            if 'checked' in el.attrib])

    def add(self, value):
        for el in self.group:
            if el.get('value') == value:
                el.set('checked', '')
                break
        else:
            raise KeyError("No checkbox with value %r" % value)

    def remove(self, value):
        for el in self.group:
            if el.get('value') == value:
                if 'checked' in el.attrib:
                    del el.attrib['checked']
                else:
                    raise KeyError(
                        "The checkbox with value %r was already unchecked" % value)
                break
        else:
            raise KeyError(
                "No checkbox with value %r" % value)

    def __repr__(self):
        return '<%s {%s} for checkboxes name=%r>' % (
            self.__class__.__name__,
            ', '.join([repr(v) for v in self]),
            self.group.name)

class InputElement(InputMixin, HtmlElement):
    """
    Represents an ``<input>`` element.

    You can get the type with ``.type`` (which is lower-cased and
    defaults to ``'text'``).

    Also you can get and set the value with ``.value``

    Checkboxes and radios have the attribute ``input.checkable ==
    True`` (for all others it is false) and a boolean attribute
    ``.checked``.
    
    """
    
    ## FIXME: I'm a little uncomfortable with the use of .checked
    def value__get(self):
        """
        Get/set the value of this element, using the ``value`` attribute.

        Also, if this is a checkbox and it has no value, this defaults
        to ``'on'``.  If it is a checkbox or radio that is not
        checked, this returns None.
        """
        if self.checkable:
            if self.checked:
                return self.get('value') or 'on'
            else:
                return None
        return self.get('value')
    def value__set(self, value):
        if self.checkable:
            if not value:
                self.checked = False
            else:
                self.checked = True
                if isinstance(value, basestring):
                    self.set('value', value)
        else:
            self.set('value', value)
    def value__del(self):
        if self.checkable:
            self.checked = False
        else:
            if 'value' in self.attrib:
                del self.attrib['value']
    value = property(value__get, value__set, value__del, doc=value__get.__doc__)

    def type__get(self):
        """
        Return the type of this element (using the type attribute).
        """
        return self.get('type', 'text').lower()
    def type__set(self, value):
        self.set('type', value)
    type = property(type__get, type__set, doc=type__get.__doc__)

    def checkable__get(self):
        """
        Boolean: can this element be checked?
        """
        return self.type in ['checkbox', 'radio']
    checkable = property(checkable__get, doc=checkable__get.__doc__)

    def checked__get(self):
        """
        Boolean attribute to get/set the presence of the ``checked``
        attribute.

        You can only use this on checkable input types.
        """
        if not self.checkable:
            raise AttributeError('Not a checkable input type')
        return 'checked' in self.attrib
    def checked__set(self, value):
        if not self.checkable:
            raise AttributeError('Not a checkable input type')
        if value:
            self.set('checked', '')
        else:
            if 'checked' in self.attrib:
                del self.attrib['checked']
    checked = property(checked__get, checked__set, doc=checked__get.__doc__)

HtmlElementClassLookup._default_element_classes['input'] = InputElement

class LabelElement(HtmlElement):
    """
    Represents a ``<label>`` element.

    Label elements are linked to other elements with their ``for``
    attribute.  You can access this element with ``label.for_element``.
    """
    
    def for_element__get(self):
        """
        Get/set the element this label points to.  Return None if it
        can't be found.
        """
        id = self.get('for')
        if not id:
            return None
        return self.body.get_element_by_id(id)
    def for_element__set(self, other):
        id = other.get('id')
        if not id:
            raise TypeError(
                "Element %r has no id attribute" % other)
        self.set('for', id)
    def for_element__del(self):
        if 'id' in self.attrib:
            del self.attrib['id']
    for_element = property(for_element__get, for_element__set, for_element__del,
                           doc=for_element__get.__doc__)

HtmlElementClassLookup._default_element_classes['label'] = LabelElement

############################################################
## Serialization
############################################################

# This isn't a general match, but it's a match for what libxml2
# specifically serialises:
__replace_meta_content_type = re.compile(
    r'<meta http-equiv="Content-Type".*?>').sub

def tostring(doc, pretty_print=False, include_meta_content_type=False,
             encoding=None):
    """
    return HTML string representation of the document given
 
    note: if include_meta_content_type is true this will create a meta
    http-equiv="Content" tag in the head; regardless of the value of include_meta_content_type
    any existing meta http-equiv="Content" tag will be removed
    """
    assert doc is not None
    html = etree.tostring(doc, method="html", pretty_print=pretty_print,
                          encoding=encoding)
    if not include_meta_content_type:
        html = __replace_meta_content_type('', html)
    return html

def open_in_browser(doc):
    """
    Open the HTML document in a web browser (saving it to a temporary
    file to open it).
    """
    import os
    import webbrowser
    try:
        write_doc = doc.write
    except AttributeError:
        write_doc = etree.ElementTree(element=doc).write
    fn = os.tempnam() + '.html'
    write_doc(fn, method="html")
    url = 'file://' + fn.replace(os.path.sep, '/')
    print url
    webbrowser.open(url)
    
################################################################################
# configure Element class lookup
################################################################################

class HTMLParser(etree.HTMLParser):
    def __init__(self, **kwargs):
        super(HTMLParser, self).__init__(**kwargs)
        self.setElementClassLookup(HtmlElementClassLookup())

def Element(*args, **kw):
    v = html_parser.makeelement(*args, **kw)
    return v

html_parser = HTMLParser()
