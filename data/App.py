import streamlit as st
import yaml
from yaml.loader import SafeLoader
import streamlit_authenticator as stauth

# Load the configuration file
with open('config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.load(file, Loader=SafeLoader)

# Create the authenticator object
authenticator = stauth.Authenticate(
    config['credentials'],
    config['cookie']['name'],
    config['cookie']['key'],
    config['cookie']['expiry_days']
)

# Display the login widget
try:
    authenticator.login( location="main")
except stauth.LoginError as e:
    st.error(e)

# Check the authentication status
if st.session_state.get("authentication_status"):
    st.sidebar.write(f"Welcome *{st.session_state.get('name')}*")
    authenticator.logout("Logout", "sidebar")

# Load roles for the authenticated user
    user_roles = st.session_state.get("roles")

    # # Example of role-based content
    if "admin" in user_roles:
        st.title("Admin Dashboard")
        st.write("Admin-specific content.")
    elif "viewer" in user_roles:
        st.title("Viewer Dashboard")
        st.write("Viewer-specific content.")
    else:
        st.title("General Dashboard")
        st.write("Content for general users.")
