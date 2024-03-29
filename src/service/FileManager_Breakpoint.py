import base64
import json
import mimetypes
import shutil
import time
from pathlib import Path
from src.service.SessionManager import SessionManager
from src.utils import HTML
from src.protocol import HTTP


def find_relative_path_to_target_folder(path, target_folder_name):
    path = Path(path).resolve()
    original_path = path
    while path.name != target_folder_name:
        if path.name == 'data':
            return True, None
        if path.parent == path:
            return False, None
        path = path.parent
    relative_path = original_path.relative_to(path.parent)
    return False, relative_path


def find_relative_path_to_root_folder(path):
    path = Path(path).resolve()
    original_path = path
    while path.name != 'data':
        if path.parent == path:
            return None
        path = path.parent
    relative_path = original_path.relative_to(path)
    return relative_path


def remove_boundary(data, boundary):
    tmp = data.split(b'\r\n--' + boundary.encode() + b'--\r\n')
    res = tmp[0]
    for i in range(1, len(tmp)):
        res += tmp[i]
    return res


def remove_double_boundary(data, boundary):
    tmp = data.split(b'\r\n--' + boundary.encode() + b'--\r\n')
    return tmp[0].split(b'\r\n\r\n', 1)[1]


def get_boundary(request):
    boundary = request.header.fields.get('Content-Type').split('boundary=')[1]
    return boundary


def build_header_only_response(dir_path, headers, request):
    file_path = Path(str(dir_path) + str(request.url))
    if file_path.is_dir():
        headers['Content-Length'] = str(len('Directory'))
        return HTTP.build_response(200, 'OK', headers, 'Directory')
    elif file_path.is_file():
        content_type = mimetypes.guess_type(file_path)[0]
        content_disposition = f'attachment; filename="{file_path.name}"'
        file_length = Path(file_path).stat().st_size
        headers['Content-Type'] = content_type
        headers['Content-Length'] = str(file_length)
        headers['Content-Disposition'] = content_disposition
        return HTTP.build_response(200, 'OK', headers)
    else:
        headers['Content-Length'] = str(len('File Not Found'))
        return HTTP.build_response(404, 'Not Found', headers, 'File Not Found')


class File_Manager:
    def __init__(self, base_path):
        self.title = "File Manager: "
        self.base_path = Path(base_path)
        self.USERS_DB = self.load_user_db()
        self.render = HTML.html_render("templates", "template.html")
        self.session_manager = SessionManager()

    def load_user_db(self):
        accounts_path = self.base_path / 'src' / 'service' / 'accounts.json'
        try:
            with accounts_path.open('r') as file:
                return json.load(file)
        except FileNotFoundError:
            return {}

    def authorize(self, auth_header, ret_username):
        encoded_credentials = auth_header.split(' ')[1]
        decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8')
        username, password = decoded_credentials.split(':', 1)
        if username in self.USERS_DB:
            if self.USERS_DB[username] == password:
                ret_username[0] = username
                return True
            else:
                return False
        else:
            return False

    def authorize_by_cookie(self, cookie):
        if self.session_manager.validate_session(cookie):
            return True
        else:
            return False

    def process(self, socket_conn, data, status: HTTP.HTTPStatus):
        headers = {}
        request = HTTP.HTTP_Request()
        headers['Content-Type'] = 'text/html'
        if status.receive_partially:
            status.current_receive_size += len(data)
            status.receive_buffer += data
            print(
                f"receive partially: {status.current_receive_size}/{status.expect_receive_size}, length of this time {len(data)}")
            if status.current_receive_size < status.expect_receive_size:
                return None
            elif status.current_receive_size == status.expect_receive_size:
                request = status.request
                request.body_without_boundary = remove_boundary(status.receive_buffer, status.boundary)
                status.receive_partially = False
                print(f"receive cost {time.time() - status.start_time} s")
                pass
            else:
                print("receive too much data, discard")
                request.body = status.receive_buffer
                status.receive_partially = False
                return None
        else:
            request = HTTP.parse_request(data)
            length = request.header.fields.get('Content-Length')
            con_info = request.header.fields.get('Connection')
            if con_info and con_info == 'close':
                status.oneshot = True
            if length and request.body and int(length) > len(request.body):
                status.receive_partially = True
                status.request = request
                status.current_receive_size = len(request.body)
                status.expect_receive_size = int(length)
                status.receive_buffer = request.body_without_boundary
                status.boundary = get_boundary(request)
                status.start_time = time.time()
                return None
            elif request.header.fields.get('Content-Type', '').startswith('multipart/form-data'):
                request.body_without_boundary = remove_double_boundary(request.body,
                                                                       get_boundary(request))
        try:
            username = ['']
            auth_header = request.header.fields.get('Authorization')
            cookie_data = request.header.fields.get('Cookie')

            if auth_header:
                if not auth_header.startswith('Basic '):
                    headers['Content-Length'] = str(len('Bad Request'))
                    return HTTP.build_response(400, 'Bad Request', headers, 'Bad Request')
                authenticated = self.authorize(auth_header, username)
                if authenticated:
                    session_id, existed = self.session_manager.create_session(username[0], cookie_data)
                    if not existed:
                        headers['Set-Cookie'] = 'session-id=' + session_id
                        headers['Max-Age'] = str(self.session_manager.SESSION_TIMEOUT)
                else:
                    out = self.render.make_login()
                    headers['Content-Length'] = str(len(out))
                    headers['WWW-Authenticate'] = 'Basic realm="Authorization Required"'
                    return HTTP.build_response(401, 'Unauthorized', headers, out)
            elif cookie_data:
                if not self.authorize_by_cookie(cookie_data):
                    out = self.render.make_login()
                    headers['Content-Length'] = str(len(out))
                    headers['WWW-Authenticate'] = 'Basic realm="Authorization Required"'
                    return HTTP.build_response(401, 'Unauthorized', headers, out)
                pass

            else:
                out = self.render.make_login()
                headers['Content-Length'] = str(len(out))
                headers['WWW-Authenticate'] = 'Basic realm="Authorization Required"'
                return HTTP.build_response(401, 'Unauthorized', headers, out)
            request.url = request.url.strip('/')
            dir_path = self.base_path / 'data'

            if request.method == 'GET':
                LIST_MODE = False
                CHUNKED_MODE = False
                is_root = request.url == ''

                if request.url.find('?') != -1:
                    relative_path, query = request.url.split('?', 1)
                    relative_path = Path(relative_path)
                    query, id = query.split('=', 1)
                    if query == 'SUSTech-HTTP' and id == '1':
                        LIST_MODE = True
                    elif query == 'SUSTech-HTTP' and id == '0':
                        LIST_MODE = False
                    elif query == 'chunked' and id == '1':
                        CHUNKED_MODE = True
                    elif query == 'chunked' and id == '0':
                        CHUNKED_MODE = False
                    else:
                        headers['Content-Length'] = str(len('Bad Request'))
                        return HTTP.build_response(400, 'Bad Request', headers, 'Bad Request')
                else:
                    relative_path = Path(request.url)

                if not (dir_path / username[0]).exists():
                    (dir_path / username[0]).mkdir()

                file_path = dir_path / relative_path
                relative_path = find_relative_path_to_root_folder(file_path)
                if relative_path is None:
                    headers['Content-Length'] = str(len('File Not Found'))
                    return HTTP.build_response(404, 'Not Found', headers, 'File Not Found')
                if file_path.name == 'favicon.ico':
                    file_path = self.base_path / 'templates/favicon.ico'
                if file_path.is_dir():
                    files_and_dirs = list(file_path.iterdir())

                    # SUSTech-HTTP
                    if LIST_MODE:
                        formatted_list = [f.name + '/' if f.is_dir() else f.name for f in files_and_dirs]
                        headers['Content-Length'] = str(len(str(formatted_list)))
                        return HTTP.build_response(200, 'OK', headers), str(formatted_list).encode()
                    else:
                        formatted_list = []
                        if not is_root:
                            formatted_list.append({"path": '/' + str(relative_path) + '/', "name": './'})
                            formatted_list.append({"path": '/' + str(relative_path.parent) + '/', "name": '../'})
                        formatted_list += [{"path": '/' + str(relative_path) + '/' + f.name + '/',
                                            "name": f.name + '/'} if f.is_dir() else {
                            "path": '/' + str(relative_path) + '/' + f.name, "name": f.name} for f in files_and_dirs]
                        out = self.render.make_main_page('/' + str(relative_path), formatted_list)
                        headers['Content-Length'] = str(len(out))
                        return HTTP.build_response(200, 'OK', headers, out)

                elif file_path.is_file():
                    content_type = mimetypes.guess_type(file_path)[0]
                    content_disposition = f'attachment; filename="{file_path.name}"'
                    with file_path.open('rb') as file:
                        file_content = file.read()
                    range_header = request.header.fields.get('Range')
                    if range_header:
                        range_list = range_header.split(',')
                        if len(range_list) == 1:
                            try:
                                start, end = range_list[0].split('-')
                                start = int(start) if start else 0
                                end = int(end) if end else len(file_content) - 1
                                if end == -1:
                                    end = len(file_content) - 1
                                if start > end or end >= len(file_content):
                                    raise ValueError("Invalid range")
                                headers['Content-Type'] = content_type
                                headers['Content-Length'] = str(end - start + 1)
                                headers['Content-Disposition'] = content_disposition
                                return HTTP.build_response(206, 'Partial Content', headers), file_content[start:end + 1]
                            except ValueError:
                                return HTTP.build_response(416, 'Range Not Satisfiable', headers,
                                                           'Range Not Satisfiable')
                        else:
                            boundary = 'THISISMYSELFDIFINEDBOUNDARY'
                            multipart_content = []

                            for range_spec in range_list:
                                try:
                                    start_str, end_str = range_spec.split('-')
                                    start = int(start_str) if start_str else 0
                                    end = int(end_str) if end_str else len(file_content) - 1
                                    if end == -1:
                                        end = len(file_content) - 1

                                    if start > end or end >= len(file_content):
                                        raise ValueError("Invalid range")

                                    range_content = file_content[start:end + 1]
                                    content_range_header = f'bytes {start}-{end}/{len(file_content)}'

                                    part_headers = f'--{boundary}\r\n'
                                    part_headers += f'Content-Type: {content_type}\r\n'
                                    part_headers += f'Content-Range: {content_range_header}\r\n\r\n'
                                    multipart_content.append(part_headers.encode() + range_content + b'\r\n')

                                except ValueError:
                                    continue

                            if multipart_content:
                                full_response = b''.join(multipart_content) + f'--{boundary}--'.encode()
                                headers['Content-Type'] = f'multipart/byteranges; boundary={boundary}'
                                headers['Content-Length'] = str(len(full_response))
                                return HTTP.build_response(206, 'Partial Content', headers), full_response
                            else:
                                return HTTP.build_response(416, 'Range Not Satisfiable', headers, 'Invalid Range')
                    headers['Content-Type'] = content_type
                    headers['Content-Length'] = str(len(file_content))
                    headers['Content-Disposition'] = content_disposition
                    return HTTP.build_response(200, 'OK', headers), file_content
                else:
                    headers['Content-Length'] = str(len('File Not Found'))
                    return HTTP.build_response(404, 'Not Found', headers, 'File Not Found')

            elif request.method == 'POST':
                if "?" not in request.url:
                    return build_header_only_response(dir_path, headers, request)
                method, relative_path = request.url.split('?', 1)
                path_flag, relative_path = relative_path.split('=', 1)
                print(f"relative_path: {relative_path}")
                if path_flag != 'path':
                    headers['Content-Length'] = str(len('Bad Request'))
                    return HTTP.build_response(400, 'Bad Request', headers, 'Bad Request')
                if relative_path[0] != '/':
                    relative_path = '/' + relative_path
                relative_path = Path(relative_path)

                file_path = Path(str(dir_path) + str(relative_path))

                is_forbidden, _ = find_relative_path_to_target_folder(file_path, username[0])
                if is_forbidden:
                    headers['Content-Length'] = str(len('Forbidden'))
                    headers['Content-Type'] = 'text/html'
                    return HTTP.build_response(403, 'Forbidden', headers, 'Forbidden')

                if method == 'upload':
                    if not request.body_without_boundary:
                        headers['Content-Length'] = str(len('No Data to Save'))
                        return HTTP.build_response(400, 'Bad Request', headers, 'No Data to Save')
                    if not file_path.is_dir():
                        headers['Content-Length'] = str(len('Invalid File Path'))
                        return HTTP.build_response(400, 'Bad Request', headers, 'Invalid File Path')
                    begin_time = time.time()
                    full_path = file_path / request.filename
                    with full_path.open('wb') as file:
                        file.write(request.body_without_boundary)
                    print(f"save cost {time.time() - begin_time} s")
                    headers['Content-Type'] = 'text/html'
                    headers['Content-Length'] = str(len('File Saved'))
                    return HTTP.build_response(200, 'OK', headers, 'File Saved')

                elif method == 'delete':
                    if not file_path.exists():
                        headers['Content-Length'] = str(len('File Not Found'))
                        return HTTP.build_response(404, 'Not Found', headers, 'File Not Found')
                    if file_path.is_dir():
                        shutil.rmtree(file_path)
                    else:
                        file_path.unlink()
                    file_path = file_path.parent
                    relative_path = relative_path.parent
                    files_and_dirs = list(file_path.iterdir())
                    formatted_list = []
                    if not (file_path.name == 'data'):
                        formatted_list.append({"path": str(relative_path), "name": '.'})
                        formatted_list.append({"path": str(relative_path.parent), "name": '..'})
                    formatted_list += [{"path": str(relative_path) + '/' + f.name, "name": f.name} for f in
                                       files_and_dirs]
                    out = self.render.make_main_page(str(relative_path), formatted_list)
                    headers['Content-Length'] = str(len(out))
                    return HTTP.build_response(200, 'OK', headers, out)
                else:
                    headers['Content-Length'] = str(len('Bad Request'))
                    return HTTP.build_response(400, 'Bad Request', headers, 'Bad Request')

            elif request.method == 'HEAD':
                return build_header_only_response(dir_path, headers, request)

            else:
                headers['Content-Length'] = str(len('Method Not Allowed'))
                return HTTP.build_response(405, 'Method Not Allowed', headers, 'Method Not Allowed')

        except Exception as e:
            print(f'file manager error: {e}')
            headers['Content-Length'] = str(len(str(e)))
            return HTTP.build_response(500, 'Internal Server Error', headers, str(e))
