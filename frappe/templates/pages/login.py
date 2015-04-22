# Copyright (c) 2013, Web Notes Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals
import frappe
import json
import frappe.utils
from frappe import _,msgprint
from phr.templates.pages.login import create_profile_in_solr,get_barcode,get_image_path


class SignupDisabledError(frappe.PermissionError): pass

no_cache = True

def get_context(context):
	# get settings from site config
	context["title"] = "Login"
	context["disable_signup"] = frappe.utils.cint(frappe.db.get_value("Website Settings", "Website Settings", "disable_signup"))

	for provider in ("google", "github", "facebook"):
		if get_oauth_keys(provider):
			context["{provider}_login".format(provider=provider)] = get_oauth2_authorize_url(provider)
			context["social_login"] = True

	return context

oauth2_providers = {
	"google": {
		"flow_params": {
			"name": "google",
			"authorize_url": "https://accounts.google.com/o/oauth2/auth",
			"access_token_url": "https://accounts.google.com/o/oauth2/token",
			"base_url": "https://www.googleapis.com",
		},

		"redirect_uri": "/api/method/frappe.templates.pages.login.login_via_google",

		"auth_url_data": {
			"scope": "https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/userinfo.email",
			"response_type": "code"
		},

		# relative to base_url
		"api_endpoint": "oauth2/v2/userinfo"
	},

	"github": {
		"flow_params": {
			"name": "github",
			"authorize_url": "https://github.com/login/oauth/authorize",
			"access_token_url": "https://github.com/login/oauth/access_token",
			"base_url": "https://api.github.com/"
		},

		"redirect_uri": "/api/method/frappe.templates.pages.login.login_via_github",

		# relative to base_url
		"api_endpoint": "user"
	},

	"facebook": {
		"flow_params": {
			"name": "facebook",
			"authorize_url": "https://www.facebook.com/dialog/oauth",
			"access_token_url": "https://graph.facebook.com/oauth/access_token",
			"base_url": "https://graph.facebook.com"
		},

		"redirect_uri": "/api/method/frappe.templates.pages.login.login_via_facebook",

		"auth_url_data": {
			"display": "page",
			"response_type": "code",
			"scope": "email,public_profile"
		},

		# relative to base_url
		"api_endpoint": "me"
	}
}

def get_oauth_keys(provider):
	"""get client_id and client_secret from database or conf"""

	# try conf
	keys = frappe.conf.get("{provider}_login".format(provider=provider))

	if not keys:
		# try database
		social = frappe.get_doc("Social Login Keys", "Social Login Keys")
		keys = {}
		for fieldname in ("client_id", "client_secret"):
			value = social.get("{provider}_{fieldname}".format(provider=provider, fieldname=fieldname))
			if not value:
				keys = {}
				break
			keys[fieldname] = value

	return keys

def get_oauth2_authorize_url(provider):
	flow = get_oauth2_flow(provider)

	# relative to absolute url
	data = { "redirect_uri": get_redirect_uri(provider) }

	# additional data if any
	data.update(oauth2_providers[provider].get("auth_url_data", {}))

	return flow.get_authorize_url(**data)

def get_oauth2_flow(provider):
	from rauth import OAuth2Service

	# get client_id and client_secret
	params = get_oauth_keys(provider)

	# additional params for getting the flow
	params.update(oauth2_providers[provider]["flow_params"])

	# and we have setup the communication lines
	return OAuth2Service(**params)

def get_redirect_uri(provider):
	redirect_uri = oauth2_providers[provider]["redirect_uri"]
	return frappe.utils.get_url(redirect_uri)

@frappe.whitelist(allow_guest=True)
def login_via_google(code):
	login_via_oauth2("google", code, decoder=json.loads)

@frappe.whitelist(allow_guest=True)
def login_via_github(code):
	login_via_oauth2("github", code)

@frappe.whitelist(allow_guest=True)
def login_via_facebook(code):
	login_via_oauth2("facebook", code)

def login_via_oauth2(provider, code, decoder=None):
	flow = get_oauth2_flow(provider)

	args = {
		"data": {
			"code": code,
			"redirect_uri": get_redirect_uri(provider),
			"grant_type": "authorization_code"
		}
	}
	if decoder:
		args["decoder"] = decoder

	session = flow.get_auth_session(**args)

	api_endpoint = oauth2_providers[provider].get("api_endpoint")
	info = session.get(api_endpoint).json()

	if "verified_email" in info and not info.get("verified_email"):
		frappe.throw(_("Email not verified with {1}").format(provider.title()))

	login_oauth_user(info, provider=provider)

def login_oauth_user(data, provider=None):

	if data.has_key("email"):
		user = data["email"]

		try:
			update_oauth_user(user, data, provider)
		except SignupDisabledError:
			return frappe.respond_as_web_page("Signup is Disabled", "Sorry. Signup from Website is disabled.",
				success=False, http_status_code=403)

		frappe.local.login_manager.user = user
		frappe.local.login_manager.post_login()

		# redirect!
		frappe.local.response["type"] = "redirect"

		# the #desktop is added to prevent a facebook redirect bug
		frappe.local.response["location"] = "/desk#desktop" if frappe.local.response.get('message') == 'Logged In' else frappe.local.response["access_link"]

		# because of a GET request!
		frappe.db.commit()
	else:
		return _("OOP'S Isuue with your Account")


def update_oauth_user(user, data, provider):
	if isinstance(data.get("location"), dict):
		data["location"] = data.get("location").get("name")

	save = False
	barcode=get_barcode()
	args={
		"person_firstname":data.get("first_name") or data.get("given_name") or data.get("name"),
		"person_middlename":"add",
		"person_lastname": data.get("last_name") or data.get("family_name"),
		"email":data.get("email"),
		"mobile":"",
		"received_from":"Desktop",
		"provider":"false",
		"barcode":str(barcode)
	}

	if not frappe.db.exists("User", user):

		# is signup disabled?
		if frappe.utils.cint(frappe.db.get_single_value("Website Settings", "disable_signup")):
			raise SignupDisabledError
		
		
		profile_res=create_profile_in_solr(args)
		response=json.loads(profile_res)
		if response['returncode']==101:
			path=get_image_path(barcode,response['entityid'])
			file_path='/files/'+response['entityid']+'/'+response['entityid']+".svg"
			save = True
			user = frappe.new_doc("User")
			user.update({
				"doctype":"User",
				"profile_id":response['entityid'],
				"first_name": data.get("first_name") or data.get("given_name") or data.get("name"),
				"last_name": data.get("last_name") or data.get("family_name"),
				"email": data["email"],
				"gender": (data.get("gender") or "").title(),
				"enabled": 1,
				"new_password": frappe.generate_hash(data["email"]),
				"location": data.get("location"),
				"user_type": "Website User",
				"access_type":"Patient",
				"user_image": data.get("picture") or data.get("avatar_url"),
				"created_via":"Desktop",
				"barcode":file_path
			})
		else:
			save = True
			user = frappe.new_doc("User")
			user.update({
				"doctype":"User",
				"first_name": data.get("first_name") or data.get("given_name") or data.get("name"),
				"last_name": data.get("last_name") or data.get("family_name"),
				"email": data["email"],
				"gender": (data.get("gender") or "").title(),
				"enabled": 1,
				"new_password": frappe.generate_hash(data["email"]),
				"location": data.get("location"),
				"user_type": "Website User",
				"user_image": data.get("picture") or data.get("avatar_url")
			})

	else:
		user = frappe.get_doc("User", user)
		save = True
		if not user.profile_id and not user.access_type=="Provider":
			profile_res=create_profile_in_solr(args)
			if response['returncode']==101:
				path=get_image_path(barcode,response['entityid'])
				file_path='/files/'+response['entityid']+'/'+response['entityid']+".svg"
				if not barcode:
					user.update({
						"barcode":file_path,
						"profile_id":response['entityid']
					})
				else:
					user.update({
						"profile_id":response['entityid']
					}) 


	if provider=="facebook" and not user.get("fb_userid"):
		save = True
		user.update({
			"fb_username": data.get("username"),
			"fb_userid": data["id"],
			"user_image": "https://graph.facebook.com/{id}/picture".format(id=data["id"])
		})

	elif provider=="google" and not user.get("google_userid"):
		save = True
		user.google_userid = data["id"]

	elif provider=="github" and not user.get("github_userid"):
		save = True
		user.github_userid = data["id"]
		user.github_username = data["login"]

	if save:
		user.ignore_permissions = True
		user.no_welcome_mail = True
		user.save()
