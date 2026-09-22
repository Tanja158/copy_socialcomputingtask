from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

# Performance: instead of running one regex per word on every text, all words of
# a tier are compiled into a single pattern once at startup. Longest entries come
# first so overlapping entries still match their longest form.
def _build_tier_pattern(words):
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in ordered) + r')\b', re.IGNORECASE)

TIER1_PATTERN = _build_tier_pattern(TIER1_WORDS)
TIER2_PATTERN = _build_tier_pattern(TIER2_PHRASES)
TIER3_PATTERN = _build_tier_pattern(TIER3_WORDS)
URL_PATTERN = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())

# ----- Design Claim: Contribution / Feedback & Rewards -----
# "Rewards, whether in the form of status, privileges, or material benefits,
#  will motivate contributions." (Kraut & Resnick, Ch. 2)
# Post-count based status badges. Ordered from highest threshold to lowest so
# the loop below returns the first (highest) tier a user qualifies for.
BADGE_TIERS = [
    (50, 'Community Pillar', 'badge-pillar'),
    (20, 'Active Member', 'badge-active'),
    (5, 'Contributor', 'badge-contributor'),
    (0, 'Newcomer', 'badge-newcomer'),
]


def get_user_badge(post_count):
    """Returns (badge_name, css_class) for a given number of posts a user has made."""
    for threshold, name, css_class in BADGE_TIERS:
        if post_count >= threshold:
            return name, css_class
    return 'Newcomer', 'badge-newcomer'


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        original_post_content = post_dict['content']
        # Design Claim (Ch. 4): approved appeals override the moderation decision
        post_dict['content'], _ = moderate_with_appeal(original_post_content, 'post', post_dict['id'])
        # Flag censored content so the author can be offered an appeal option
        post_dict['was_censored'] = (post_dict['content'] != original_post_content)
        post_dict['has_appeal'] = has_appeal('post', post_dict['id'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            original_comment_content = comment_dict['content']
            comment_dict['content'], _ = moderate_with_appeal(original_comment_content, 'comment', comment_dict['id'])
            comment_dict['was_censored'] = (comment_dict['content'] != original_comment_content)
            comment_dict['has_appeal'] = has_appeal('comment', comment_dict['id'])
            comments_moderated.append(comment_dict)

        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  3b. Design Claim (Commitment): "Displaying photos and information about
    #  individual members and their recent activities will promote bond-based
    #  commitment." (Kraut & Resnick, Ch. 3)
    #  Show a small widget with the latest posts from people the user follows,
    #  independent of the sort/filter above, to remind them to check in.
    followed_activity = []
    following_count = 0
    if current_user_id:
        following_count = query_db(
            'SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?',
            (current_user_id,), one=True
        )['cnt']
        activity_raw = query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)
            ORDER BY p.created_at DESC
            LIMIT 5
        ''', (current_user_id,))
        for activity_post in activity_raw:
            activity_dict = dict(activity_post)
            activity_dict['content'], _ = moderate_content(activity_dict['content'])
            followed_activity.append(activity_dict)

    #  4. Render Template with Pagination Info 
        #  3c. Design Claims (Ch. 5): newcomers are asked to introduce themselves,
    #  and everyone sees who recently joined so they can greet them early.
    ensure_newcomer_tables()
    show_intro_prompt = bool(current_user_id and not has_introduction(current_user_id))
    new_members_raw = query_db('''
        SELECT id, username, created_at FROM users ORDER BY created_at DESC LIMIT 5
    ''')
    new_members = []
    for member_raw in new_members_raw:
        member = dict(member_raw)
        member['is_newcomer'] = account_age_days(member['created_at']) < NEWCOMER_DAYS
        new_members.append(member)
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           followed_activity=followed_activity,
                           following_count=following_count,
                           show_intro_prompt=show_intro_prompt,
                           new_members=new_members)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Pass the user's content through the moderation function
    moderated_content = content

    # Basic validation to ensure post is not empty
    if moderated_content and moderated_content.strip():
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, moderated_content))
        db.commit()
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    # Design Claim (Contribution): status badge based on how many posts this user has made
    post_count = len(posts)
    badge_name, badge_class = get_user_badge(post_count)

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following,
                           post_count=post_count,
                           badge_name=badge_name,
                           badge_class=badge_class)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        flash('Your comment was added.', 'success')
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring
        # --- Appeals Tab Data (Design Claim Ch. 4: appeal procedures) ---
    ensure_appeals_table()
    appeals_raw = query_db('''
        SELECT a.id, a.user_id, a.content_type, a.content_id, a.reason, a.status,
               a.admin_response, a.created_at, a.resolved_at, u.username
        FROM appeals a JOIN users u ON a.user_id = u.id
        ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.created_at DESC
    ''')
    appeals = []
    for appeal_raw in appeals_raw:
        appeal = dict(appeal_raw)
        table = 'posts' if appeal['content_type'] == 'post' else 'comments'
        item = query_db(f'SELECT content FROM {table} WHERE id = ?', (appeal['content_id'],), one=True)
        appeal['original_content'] = item['content'] if item else '(content deleted)'
        # Show what the automated system did with it, so the decision is transparent
        appeal['moderated_content'], appeal['content_score'] = moderate_content(appeal['original_content'])
        appeals.append(appeal)

    pending_appeals_count = sum(1 for a in appeals if a['status'] == 'pending')


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           appeals=appeals,
                           pending_appeals_count=pending_appeals_count,
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

# Design Claim (Chapter 4 - Regulating Behavior in Online Communities):
# "Consistently applied moderation criteria, a chance to argue one's case, and
#  appeal procedures increase the legitimacy and thus the effectiveness of
#  moderation decisions."
#
# Implemented as an appeal procedure: when a user's own post or comment has been
# censored by the automated moderation system, they can submit a written appeal.
# An administrator reviews each appeal in the admin dashboard and either
# approves it (the original content is restored and shown uncensored) or rejects
# it with a written explanation. Users can track the status of their appeals.


def ensure_appeals_table():
    """
    Creates the 'appeals' table if it does not exist yet, so the feature also
    works on a fresh copy of the database without a manual migration step.
    """
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS appeals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content_type TEXT NOT NULL,       -- 'post' or 'comment'
            content_id INTEGER NOT NULL,
            reason TEXT NOT NULL,             -- the user's own argument
            status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
            admin_response TEXT,              -- the moderator's explanation
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP
        )
    ''')
    db.commit()


def is_appeal_approved(content_type, content_id):
    """Returns True if an approved appeal exists for this post/comment."""
    ensure_appeals_table()
    row = query_db(
        "SELECT 1 FROM appeals WHERE content_type = ? AND content_id = ? AND status = 'approved'",
        (content_type, content_id), one=True
    )
    return row is not None


def has_appeal(content_type, content_id):
    """Returns the status of an existing appeal for this content, or None."""
    ensure_appeals_table()
    row = query_db(
        'SELECT status FROM appeals WHERE content_type = ? AND content_id = ?',
        (content_type, content_id), one=True
    )
    return row['status'] if row else None


def moderate_with_appeal(content, content_type, content_id):
    """
    Wrapper around moderate_content() that respects approved appeals.
    If a moderator has approved an appeal for this piece of content, the original
    (uncensored) text is shown and the score is reset to 0.0 - the moderation
    decision has been formally overturned.
    """
    if is_appeal_approved(content_type, content_id):
        return content, 0.0
    return moderate_content(content)


@app.route('/appeals/new/<content_type>/<int:content_id>', methods=['POST'])
def submit_appeal(content_type, content_id):
    """Lets the author of a censored post or comment argue their case."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to appeal a moderation decision.', 'danger')
        return redirect(url_for('login'))

    if content_type not in ('post', 'comment'):
        flash('Invalid appeal type.', 'danger')
        return redirect(url_for('feed'))

    ensure_appeals_table()

    # Security check: users may only appeal their own content
    table = 'posts' if content_type == 'post' else 'comments'
    item = query_db(f'SELECT user_id FROM {table} WHERE id = ?', (content_id,), one=True)
    if not item:
        flash('That content no longer exists.', 'danger')
        return redirect(url_for('feed'))
    if item['user_id'] != user_id:
        flash('You can only appeal moderation decisions on your own content.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Prevent duplicate appeals for the same piece of content
    existing = query_db(
        'SELECT id, status FROM appeals WHERE content_type = ? AND content_id = ?',
        (content_type, content_id), one=True
    )
    if existing:
        flash(f'You have already appealed this {content_type} (status: {existing["status"]}).', 'info')
        return redirect(url_for('my_appeals'))

    reason = (request.form.get('reason') or '').strip()
    if not reason:
        flash('Please explain why you think this moderation decision was wrong.', 'warning')
        return redirect(request.referrer or url_for('feed'))

    db = get_db()
    db.execute(
        'INSERT INTO appeals (user_id, content_type, content_id, reason) VALUES (?, ?, ?, ?)',
        (user_id, content_type, content_id, reason)
    )
    db.commit()
    flash('Your appeal has been submitted and will be reviewed by a moderator.', 'success')
    return redirect(url_for('my_appeals'))


@app.route('/appeals')
def my_appeals():
    """Shows the current user their own appeals and how they were decided."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to view your appeals.', 'danger')
        return redirect(url_for('login'))

    ensure_appeals_table()
    appeals_raw = query_db('''
        SELECT id, content_type, content_id, reason, status, admin_response, created_at, resolved_at
        FROM appeals WHERE user_id = ? ORDER BY created_at DESC
    ''', (user_id,))

    appeals = []
    for appeal_raw in appeals_raw:
        appeal = dict(appeal_raw)
        # Fetch the original content so the user can see what they appealed
        table = 'posts' if appeal['content_type'] == 'post' else 'comments'
        item = query_db(f'SELECT content FROM {table} WHERE id = ?', (appeal['content_id'],), one=True)
        appeal['original_content'] = item['content'] if item else '(content deleted)'
        appeals.append(appeal)

    return render_template('appeals.html.j2', appeals=appeals)


@app.route('/admin/appeals/<int:appeal_id>/<decision>', methods=['POST'])
def resolve_appeal(appeal_id, decision):
    """Lets an administrator approve or reject an appeal, with a written reason."""
    if session.get('username') != 'admin':
        flash('You do not have permission to perform this action.', 'danger')
        return redirect(url_for('feed'))

    if decision not in ('approved', 'rejected'):
        flash('Invalid decision.', 'danger')
        return redirect(url_for('admin_dashboard', tab='appeals'))

    ensure_appeals_table()
    admin_response = (request.form.get('admin_response') or '').strip()

    db = get_db()
    db.execute('''
        UPDATE appeals SET status = ?, admin_response = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (decision, admin_response, appeal_id))
    db.commit()

    flash(f'Appeal {appeal_id} has been {decision}.', 'success')
    return redirect(url_for('admin_dashboard', tab='appeals'))



# Design Claims (Chapter 5 - The Challenges of Dealing with Newcomers)
#
# Claim 1 (Retention 3): "Encouraging newcomers to reveal themselves publicly
#   in profiles or 'introduction threads' gives existing group members a basis
#   for conversation with newcomers and therefore should increase interaction
#   between old timers and newcomers."
#   -> Newcomers are asked to post an introduction, which is collected on a
#      public "Introductions" page.
#
# Claim 2 (Retention 2): "When newcomers have friendly interactions with
#   existing community members soon after joining a community, they will be
#   more likely to stay and contribute more."
#   -> Existing members see who recently joined and can welcome them with a
#      short message right on the introduction.

# A member counts as a newcomer during their first two weeks.
NEWCOMER_DAYS = 14


def ensure_newcomer_tables():
    """
    Creates the tables for introductions and welcome messages if they do not
    exist yet, so the feature also works on a fresh copy of the database.
    """
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS introductions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS welcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            introduction_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
        )
    ''')
    db.commit()


def account_age_days(created_at):
    """How many days ago an account was created."""
    if not created_at:
        return 9999
    return (datetime.utcnow() - created_at).days


def has_introduction(user_id):
    """True if this user already posted an introduction."""
    ensure_newcomer_tables()
    row = query_db('SELECT 1 FROM introductions WHERE user_id = ?', (user_id,), one=True)
    return row is not None


@app.route('/introductions')
def introductions():
    """
    Design Claim 1: the public introduction thread. Newcomers introduce
    themselves here, and existing members get a basis for starting a
    conversation with them.
    """
    ensure_newcomer_tables()

    intros_raw = query_db('''
        SELECT i.id, i.content, i.created_at, u.id as user_id, u.username,
               u.location, u.created_at as joined_at
        FROM introductions i JOIN users u ON i.user_id = u.id
        ORDER BY i.created_at DESC LIMIT 25
    ''')

    current_user_id = session.get('user_id')
    intros = []
    for intro_raw in intros_raw:
        intro = dict(intro_raw)
        # Introductions are user content, so they pass through moderation too
        intro['content'], _ = moderate_content(intro['content'])
        intro['is_newcomer'] = account_age_days(intro['joined_at']) < NEWCOMER_DAYS

        welcomes_raw = query_db('''
            SELECT w.message, w.created_at, u.username
            FROM welcomes w JOIN users u ON w.user_id = u.id
            WHERE w.introduction_id = ? ORDER BY w.created_at ASC
        ''', (intro['id'],))
        welcomes = []
        for welcome_raw in welcomes_raw:
            welcome = dict(welcome_raw)
            welcome['message'], _ = moderate_content(welcome['message'])
            welcomes.append(welcome)
        intro['welcomes'] = welcomes

        # A member should not welcome themselves, and only once per newcomer
        intro['can_welcome'] = bool(
            current_user_id
            and current_user_id != intro['user_id']
            and not query_db(
                'SELECT 1 FROM welcomes WHERE introduction_id = ? AND user_id = ?',
                (intro['id'], current_user_id), one=True)
        )
        intros.append(intro)

    # Who joined recently - this is what makes early friendly contact possible
    new_members = query_db('''
        SELECT id, username, created_at FROM users
        ORDER BY created_at DESC LIMIT 5
    ''')

    already_introduced = bool(current_user_id and has_introduction(current_user_id))

    return render_template('introductions.html.j2',
                           intros=intros,
                           new_members=new_members,
                           already_introduced=already_introduced)


@app.route('/introductions/new', methods=['POST'])
def add_introduction():
    """Lets a member post their own introduction (only one per member)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to introduce yourself.', 'danger')
        return redirect(url_for('login'))

    ensure_newcomer_tables()

    if has_introduction(user_id):
        flash('You have already introduced yourself.', 'info')
        return redirect(url_for('introductions'))

    content = (request.form.get('content') or '').strip()
    if not content:
        flash('Please write a few words about yourself.', 'warning')
        return redirect(url_for('introductions'))

    db = get_db()
    db.execute('INSERT INTO introductions (user_id, content) VALUES (?, ?)',
               (user_id, content))
    db.commit()
    flash('Thanks for introducing yourself! Other members can now welcome you.', 'success')
    return redirect(url_for('introductions'))


@app.route('/introductions/<int:introduction_id>/welcome', methods=['POST'])
def welcome_newcomer(introduction_id):
    """
    Design Claim 2: existing members greet a newcomer shortly after they
    joined, which makes the newcomer more likely to stay.
    """
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to welcome someone.', 'danger')
        return redirect(url_for('login'))

    ensure_newcomer_tables()

    intro = query_db('SELECT user_id FROM introductions WHERE id = ?',
                     (introduction_id,), one=True)
    if not intro:
        flash('That introduction no longer exists.', 'danger')
        return redirect(url_for('introductions'))
    if intro['user_id'] == user_id:
        flash('You cannot welcome yourself.', 'warning')
        return redirect(url_for('introductions'))

    # One welcome per member per newcomer
    if query_db('SELECT 1 FROM welcomes WHERE introduction_id = ? AND user_id = ?',
                (introduction_id, user_id), one=True):
        flash('You have already welcomed this member.', 'info')
        return redirect(url_for('introductions'))

    message = (request.form.get('message') or '').strip()
    if not message:
        message = 'Welcome to Mini Social!'

    db = get_db()
    db.execute('INSERT INTO welcomes (introduction_id, user_id, message) VALUES (?, ?, ?)',
               (introduction_id, user_id, message))
    db.commit()
    flash('Your welcome message was posted.', 'success')
    return redirect(url_for('introductions'))

@app.route('/leaderboard')
def leaderboard():
    """
    Design Claim (Contribution / Feedback & Rewards): "Comparative performance
    feedback can enhance motivation, as long as high-performance is viewed as
    desirable and potentially obtainable." (Kraut & Resnick, Ch. 2)

    Ranks users by number of posts, then by total reactions received on
    those posts, and shows their status badge.
    """
    rows = query_db('''
        SELECT u.id, u.username,
               COUNT(DISTINCT p.id) as post_count,
               COUNT(r.id) as reaction_count
        FROM users u
        LEFT JOIN posts p ON p.user_id = u.id
        LEFT JOIN reactions r ON r.post_id = p.id
        GROUP BY u.id
        HAVING post_count > 0
        ORDER BY post_count DESC, reaction_count DESC
        LIMIT 10
    ''')

    leaders = []
    for row in rows:
        leader = dict(row)
        badge_name, badge_class = get_user_badge(leader['post_count'])
        leader['badge_name'] = badge_name
        leader['badge_class'] = badge_class
        leaders.append(leader)

    return render_template('leaderboard.html.j2',
                           leaders=leaders,
                           current_user_id=session.get('user_id'))

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below
# Coding Assignment #2

# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    # Step 1: Profile Score
    _, profile_score = moderate_content(user['profile'] or '')

    # Step 2: Post Score (average Content Score across all of the user's posts)
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    if posts:
        average_post_score = sum(moderate_content(p['content'])[1] for p in posts) / len(posts)
    else:
        average_post_score = 0.0

    # Step 3: Comment Score (average Content Score across all of the user's comments)
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    if comments:
        average_comment_score = sum(moderate_content(c['content'])[1] for c in comments) / len(comments)
    else:
        average_comment_score = 0.0

    # Step 4: Combine Scores
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

     # Step 5: Apply Age Multiplier
    account_age_days = (datetime.utcnow() - user['created_at']).days
    if account_age_days < 7:
        user_risk_score = content_risk_score * 1.5
    elif account_age_days < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: Final Capping
    return min(user_risk_score, 5.0)
    

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)
    """
    if not content:
        return content, 0.0

    # ----- Stage 1.1: Severe Violation Checks -----
    # Rule 1.1.1: Tier 1 words -> case-insensitive, whole-word match
    if TIER1_PATTERN.search(content):
        return '[content removed due to severe violation]', 5.0

    # Rule 1.1.2: Tier 2 phrases -> case-insensitive, whole-phrase match
    if TIER2_PATTERN.search(content):
        return '[content removed due to spam/scam policy]', 5.0

    # ----- Stage 1.2: Scored Violations & Filtering -----
    moderated_content = content
    score = 0.0

    # Rule 1.2.1: Tier 3 words -> replaced with asterisks of equal length, +2.0 each
    tier3_matches = TIER3_PATTERN.findall(moderated_content)
    if tier3_matches:
        score += 2.0 * len(tier3_matches)
        moderated_content = TIER3_PATTERN.sub(lambda m: '*' * len(m.group(0)), moderated_content)

    # Rule 1.2.2: External links -> replaced with '[link removed]', +2.0 each
    urls = URL_PATTERN.findall(moderated_content)
    if urls:
        score += 2.0 * len(urls)
        moderated_content = URL_PATTERN.sub('[link removed]', moderated_content)

    # Rule 1.2.3: Excessive capitalization -> flat +0.5, content is not modified
    alpha_chars = [c for c in moderated_content if c.isalpha()]
    if len(alpha_chars) > 15:
        upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if upper_ratio > 0.7:
            score += 0.5

    return moderated_content, score

# Coding Assignment #3
# Common English words that appear in almost every post. They say nothing about
# what a post is about, so they are ignored when comparing posts.
STOP_WORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'been', 'but', 'by', 'can',
    'did', 'do', 'does', 'for', 'from', 'get', 'got', 'had', 'has', 'have',
    'he', 'her', 'here', 'his', 'how', 'i', 'if', 'in', 'is', 'it', 'its',
    'just', 'me', 'my', 'no', 'not', 'of', 'on', 'one', 'or', 'our', 'out',
    'she', 'so', 'some', 'that', 'the', 'their', 'them', 'then', 'there',
    'these', 'they', 'this', 'to', 'up', 'us', 'very', 'was', 'we', 'were',
    'what', 'when', 'which', 'who', 'will', 'with', 'would', 'you', 'your',
    'about', 'after', 'all', 'also', 'am', 'any', 'because', 'before', 'being',
    'good', 'great', 'like', 'more', 'much', 'new', 'now', 'really', 'today',
    'too', 'want', 'way', 'well', 'went', 'why', 'work', 'made', 'make',
}


def extract_keywords(text):
    """
    Turns a post into a list of meaningful words, so two posts can be compared
    by the words they share. Everything is lowercased, punctuation is dropped,
    and very short or very common words are filtered out.
    """
    if not text:
        return []
    words = re.findall(r'[a-zA-Z]+', text.lower())
    return [w for w in words if len(w) >= 3 and w not in STOP_WORDS]
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """
        # A user who is not logged in has no history to base recommendations on,
    # so we simply fall back to the newest posts.
    if not user_id:
        return query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p JOIN users u ON p.user_id = u.id
            ORDER BY p.created_at DESC LIMIT 5
        ''')

    # --- Step 1: build the user's interest profile ---------------------------
    # Posts the user reacted to positively. 'sad' and 'angry' are left out,
    # because they express dislike rather than interest.
    liked_posts = query_db('''
        SELECT p.id, p.content, p.user_id
        FROM reactions r JOIN posts p ON r.post_id = p.id
        WHERE r.user_id = ? AND r.reaction_type IN ('like', 'love', 'laugh', 'wow')
    ''', (user_id,))

    # Count how often each keyword appears in the content the user liked.
    # The more often a word shows up, the more it represents their interests.
    interest_keywords = {}
    for post in liked_posts:
        for word in extract_keywords(post['content']):
            interest_keywords[word] = interest_keywords.get(word, 0) + 1

    # Authors the user follows, and authors whose posts they liked before.
    followed_ids = {row['followed_id'] for row in query_db(
        'SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}
    liked_author_ids = {post['user_id'] for post in liked_posts}
    seen_post_ids = {post['id'] for post in liked_posts}

    # --- Step 2: collect candidate posts ------------------------------------
    # Only recent posts are considered, so recommendations stay up to date.
    # The user's own posts and posts they already reacted to are excluded.
    if filter_following:
        if not followed_ids:
            return []
        candidates = query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p JOIN users u ON p.user_id = u.id
            WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)
              AND p.user_id != ?
            ORDER BY p.created_at DESC LIMIT 1000
        ''', (user_id, user_id))
    else:
        candidates = query_db('''
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p JOIN users u ON p.user_id = u.id
            WHERE p.user_id != ?
            ORDER BY p.created_at DESC LIMIT 1000
        ''', (user_id,))

    # --- Step 3: score every candidate --------------------------------------
    scored_posts = []
    for post in candidates:
        if post['id'] in seen_post_ids:
            continue

        score = 0.0

        # Content similarity is the main signal: every keyword the post shares
        # with the interest profile adds points, weighted by how often that
        # word appeared in the content the user liked.
        for word in set(extract_keywords(post['content'])):
            if word in interest_keywords:
                score += 2.0 * interest_keywords[word]

        # Social signals only act as a smaller bonus. Otherwise every post of a
        # followed user would outrank posts that actually match the interests.
        if post['user_id'] in followed_ids:
            score += 1.0
        if post['user_id'] in liked_author_ids:
            score += 0.5

        if score > 0:
            scored_posts.append((score, post))

    # If the user has no history yet, nothing gets a score. In that case we
    # show the newest posts instead of an empty tab.
    if not scored_posts:
        return candidates[:5]

    # --- Step 4: take the 5 best posts, newest first -------------------------
    scored_posts.sort(key=lambda item: item[0], reverse=True)
    best_posts = [post for _, post in scored_posts[:5]]
    best_posts.sort(key=lambda post: post['created_at'], reverse=True)

    return best_posts
    

if __name__ == '__main__':
    app.run(debug=True, port=8080)

